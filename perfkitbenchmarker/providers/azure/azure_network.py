# Copyright 2014 PerfKitBenchmarker Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Module containing classes related to Azure VM networking.

The Firewall class provides a way of opening VM ports. The Network class allows
VMs to communicate via internal ips and isolates PerfKitBenchmarker VMs from
others in
the same project. See http://msdn.microsoft.com/library/azure/jj156007.aspx
for more information about Azure Virtual Networks.
"""

import logging
import json
import threading

from perfkitbenchmarker import context
from perfkitbenchmarker import errors
from perfkitbenchmarker import flags
from perfkitbenchmarker import network
from perfkitbenchmarker import resource
from perfkitbenchmarker import vm_util
from perfkitbenchmarker import providers
from perfkitbenchmarker.providers import azure

FLAGS = flags.FLAGS
SSH_PORT = 22

DEFAULT_LOCATION = 'eastus2'


def GetResourceGroup():
  """Get the resource group for the current benchmark."""
  spec = context.GetThreadBenchmarkSpec()
  # This is protected by spec.networks_lock, so there's no race
  # condition with checking for the attribute and then creating a
  # resource group.
  try:
    return spec.azure_resource_group
  except AttributeError:
    group = AzureResourceGroup(FLAGS.run_uri, spec.uid)
    spec.azure_resource_group = group
    return group


class AzureResourceGroup(resource.BaseResource):
  """A Resource Group, the basic unit of Azure provisioning."""

  def __init__(self, run_uri, spec_uid):
    super(AzureResourceGroup, self).__init__()
    self.name = 'pkb%s-%s' % (run_uri, spec_uid)
    # Storage account names can't include separator characters :(.
    self.storage_account_prefix = 'pkb%s%s' % (run_uri, spec_uid)
    # A resource group's location doesn't affect the location of
    # actual resources, but we need to choose *some* region for every
    # benchmark, even if the user doesn't specify one.
    self.location = FLAGS.zones[0] if FLAGS.zones else DEFAULT_LOCATION
    # Whenever an Azure CLI command needs a resource group, it's
    # always specified the same way.
    self.args = ['--resource-group', self.name]

  def _Create(self):
    # A resource group can own resources in multiple zones, but the
    # group itself needs to have a location. Therefore,
    # FLAGS.zones[0].
    _, _, retcode = vm_util.IssueCommand(
        [azure.AZURE_PATH, 'group', 'create',
         self.name, self.location])

    if retcode:
      raise errors.RetryableCreationError(
          'Error creating Azure resource group')

  def _Exists(self):
    _, _, retcode = vm_util.IssueCommand(
        [azure.AZURE_PATH, 'resource', 'list', self.name])

    return retcode == 0

  def _Delete(self):
    vm_util.IssueCommand(
        [azure.AZURE_PATH, 'group', 'delete',
         '--quiet',
         self.name])


class AzureStorageAccount(resource.BaseResource):
  """Object representing an Azure Storage Account."""

  def __init__(self, storage_type, location, name,
               kind=None, access_tier=None):
    super(AzureStorageAccount, self).__init__()
    self.storage_type = storage_type
    self.name = name
    self.resource_group = GetResourceGroup()
    self.location = location
    self.kind = kind or 'Storage'

    if kind == 'BlobStorage':
      self.access_tier = access_tier or 'Hot'
    else:
      # Access tiers are only valid for blob storage accounts.
      assert access_tier is None
      self.access_tier = access_tier

  def _Create(self):
    """Creates the storage account."""
    create_cmd = [azure.AZURE_PATH,
                  'storage',
                  'account',
                  'create',
                  '--kind', self.kind,
                  '--sku-name', self.storage_type,
                  self.name] + self.resource_group.args
    if self.location:
      create_cmd.extend(
          ['--location', self.location])
    if self.kind == 'BlobStorage':
      create_cmd.extend(
          ['--access-tier', self.access_tier])
    vm_util.IssueCommand(create_cmd)

  def _PostCreate(self):
    """Get our connection string and our keys."""

    stdout, _ = vm_util.IssueRetryableCommand(
        [azure.AZURE_PATH, 'storage', 'account', 'connectionstring', 'show',
         '--json',
         self.name] + self.resource_group.args)

    response = json.loads(stdout)
    self.connection_string = response['string']
    # Connection strings are always represented the same way on the
    # command line.
    self.connection_args = ['--connection-string', self.connection_string]

    stdout, _ = vm_util.IssueRetryableCommand(
        [azure.AZURE_PATH, 'storage', 'account', 'keys', 'list',
         '--json',
         self.name] + self.resource_group.args)

    response = json.loads(stdout)
    # A new storage account comes with two keys, but we only need one.
    assert response[0]['permissions'] == 'Full'
    self.key = response[0]['value']

  def _Delete(self):
    """Deletes the storage account."""
    vm_util.IssueCommand(
        [azure.AZURE_PATH, 'storage', 'account', 'delete',
         '--quiet',
         self.name] + self.resource_group.args)

  def _Exists(self):
    """Returns true if the storage account exists."""
    _, _, retcode = vm_util.IssueCommand(
        [azure.AZURE_PATH, 'storage', 'account', 'show',
         '--json',
         self.name] + self.resource_group.args,
        suppress_warning=True)

    return retcode == 0


class AzureVirtualNetwork(resource.BaseResource):
  """Object representing an Azure Virtual Network."""

  num_vnets = 0
  vnet_lock = threading.Lock()

  def __init__(self, location, name):
    super(AzureVirtualNetwork, self).__init__()
    self.name = name
    self.resource_group = GetResourceGroup()
    self.location = location
    self.args = ['--vnet-name', self.name]

    with self.vnet_lock:
      self.vnet_num = self.num_vnets
      self.__class__.num_vnets += 1

    # Allocate a different /16 in each region. This allows for 255
    # regions (should be enough for anyone), and 65536 VMs in each
    # region. Using different address spaces prevents us from
    # accidentally contacting the wrong VM.
    self.address_space = '10.%s.0.0/16' % self.vnet_num

  def _Create(self):
    """Creates the virtual network."""
    vm_util.IssueCommand(
        [azure.AZURE_PATH, 'network', 'vnet', 'create',
         '--location', self.location,
         '--address-prefixes', self.address_space,
         self.name] + self.resource_group.args)

  def _Delete(self):
    """Deletes the virtual network."""
    vm_util.IssueCommand(
        [azure.AZURE_PATH, 'network', 'vnet', 'delete',
         '--quiet',
         self.name] + self.resource_group.args)

  def _Exists(self):
    """Returns true if the virtual network exists."""
    stdout, _, retcode = vm_util.IssueCommand(
        [azure.AZURE_PATH, 'network', 'vnet', 'show',
         '--json',
         self.name] + self.resource_group.args,
        suppress_warning=True)

    return retcode == 0 and stdout != '{}\n'


class AzureSubnet(resource.BaseResource):
  def __init__(self, vnet, name):
    super(AzureSubnet, self).__init__()
    self.resource_group = GetResourceGroup()
    self.vnet = vnet
    self.name = name
    self.args = ['--vnet-subnet-name', self.name]

  def _Create(self):
    logging.info('subnet._Create')
    vm_util.IssueCommand(
        [azure.AZURE_PATH, 'network', 'vnet', 'subnet', 'create',
         '--vnet-name', self.vnet.name,
         '--address-prefix', self.vnet.address_space,
         self.name] + self.resource_group.args)

  def _Exists(self):
    stdout, _, retcode = vm_util.IssueCommand(
        [azure.AZURE_PATH, 'network', 'vnet', 'subnet', 'show',
         '--vnet-name', self.vnet.name,
         '--json',
         self.name] + self.resource_group.args)

    return retcode == 0 and stdout != '{}\n'

  def _Delete(self):
    vm_util.IssueCommand(
        [azure.AZURE_PATH, 'network', 'vnet', 'subnet', 'delete',
         '--vnet-name', self.vnet.name,
         '--quiet',
         self.name] + self.resource_group.args)


class AzureNetworkSecurityGroup(resource.BaseResource):
  def __init__(self, location, subnet, name):
    super(AzureNetworkSecurityGroup, self).__init__()

    self.location = location
    self.subnet = subnet
    self.name = name
    self.resource_group = GetResourceGroup()
    self.args = ['--nsg-name', self.name]

    self.rules_lock = threading.Lock()
    # Mapping of (start_port, end_port) -> rule name, used to
    # deduplicate rules. We expect duplicate rules because PKB will
    # call AllowPort() for each VM on a subnet, but the rules are
    # actually applied to the entire subnet.
    self.rules = {}
    # True if the special 'DenyAll' rule is present.
    self.have_deny_all_rule = False

  def _Create(self):
    vm_util.IssueCommand(
        [azure.AZURE_PATH, 'network', 'nsg', 'create',
         '--location', self.location,
         self.name] + self.resource_group.args)

  def _Exists(self):
    stdout, _, retcode = vm_util.IssueCommand(
        [azure.AZURE_PATH, 'network', 'nsg', 'show',
         '--json',
         self.name] + self.resource_group.args)

    return retcode == 0 and stdout != '{}\n'

  def _Delete(self):
    vm_util.IssueCommand(
        [azure.AZURE_PATH, 'network', 'nsg', 'delete',
         '--quiet',
         self.name] + self.resource_group.args)

  def AttachToSubnet(self):
    vm_util.IssueRetryableCommand(
        [azure.AZURE_PATH, 'network', 'vnet', 'subnet', 'set',
         '--name', self.subnet.name,
         '--network-security-group-name', self.name] +
        self.resource_group.args +
        self.subnet.vnet.args)

  def AllowPort(self, vm, start_port, end_port=None, source_range=None):
    """Open a port or port range.

    Args:
      vm: the virtual machine to open the port for.
      start_port: either a single port or the start of a range.
      end_port: if given, the end of the port range.
      source_range: unsupported at present.
    """

    with self.rules_lock:
      end_port = end_port or start_port

      if (start_port, end_port) in self.rules:
        return
      port_range = '%s-%s' % (start_port, end_port)
      rule_name = 'allow-%s' % port_range
      # Azure priorities are between 100 and 4096, but we reserve 4095
      # for the special DenyAll rule created by DisallowAllPorts.
      rule_priority = 100 + len(self.rules)
      if rule_priority >= 4095:
        raise ValueError('Too many firewall rules!')
      self.rules[(start_port, end_port)] = rule_name

    vm_util.IssueRetryableCommand(
        [azure.AZURE_PATH, 'network', 'nsg', 'rule', 'create',
         rule_name,
         '--destination-port-range', port_range,
         '--access', 'Allow',
         '--priority', str(rule_priority)]
        + self.resource_group.args
        + self.args)

  def DisallowAllPorts(self):
    with self.rules_lock:
      for port, rule in self.rules.iteritems():
        vm_util.IssueRetryableCommand(
            [azure.AZURE_PATH, 'network', 'nsg', 'rule', 'delete',
             '--quiet',
             rule] +
            self.resource_group.args +
            self.args)
      # Can't remove rules from self.rules in the for loop because the
      # iterator errors when the underlying dictionary is changed
      # during iteration.
      self.rules = {}

      if self.have_deny_all_rule:
        return
      # An NSG comes with some default rules, which we can't delete,
      # so we create a new rule at higher priority than those.
      vm_util.IssueRetryableCommand(
          [azure.AZURE_PATH, 'network', 'nsg', 'rule', 'create',
           'DenyAll',
           '--access', 'Deny',
           '--priority', '4095']
          + self.resource_group.args
          + self.args)
      self.have_deny_all_rule = True


ALL_NSGS = set()


class AzureFirewall(network.BaseFirewall):
  """A fireall on Azure is a Network Security Group.

  NSGs are per-subnet, but this class is per-provider, so we just
  proxy methods through to the right NSG instance.
  """

  CLOUD = providers.AZURE

  def AllowPort(self, vm, start_port, end_port=None, source_range=None):
    """Opens a port on the firewall.

    Args:
      vm: The BaseVirtualMachine object to open the port for.
      start_port: The local port to open.
      end_port: if given, open the range [start_port, end_port].
    """

    vm.network.nsg.AllowPort(vm, start_port, end_port=end_port,
                             source_range=source_range)

  def DisallowAllPorts(self):
    """Closes all ports on the firewall."""

    for nsg in ALL_NSGS:
      nsg.DisallowAllPorts()


class AzureNetwork(network.BaseNetwork):
  """Regional network components.

  A container object holding all of the network-related objects that
  we need for an Azure zone (aka region).
  """

  CLOUD = providers.AZURE

  def __init__(self, spec):
    super(AzureNetwork, self).__init__(spec)
    self.resource_group = GetResourceGroup()

    # Storage account names must be 3-24 characters long and use
    # numbers and lower-case letters only, which leads us to this
    # awful naming scheme.
    suffix = '%sstorage' % self.zone
    self.storage_account = AzureStorageAccount(
        FLAGS.azure_storage_type, self.zone,
        self.resource_group.storage_account_prefix[:24 - len(suffix)] + suffix)
    prefix = '%s-%s' % (self.resource_group.name, self.zone)
    self.vnet = AzureVirtualNetwork(self.zone, prefix + '-vnet')
    self.subnet = AzureSubnet(self.vnet, self.vnet.name + '-subnet')
    self.nsg = AzureNetworkSecurityGroup(self.zone, self.subnet,
                                         self.subnet.name + '-nsg')
    ALL_NSGS.add(self.nsg)

  @vm_util.Retry()
  def Create(self):
    """Creates the network."""
    # If the benchmark includes multiple zones,
    # self.resource_group.Create() will be called more than once. But
    # BaseResource will prevent us from running the underlying Azure
    # commands more than once, so that is fine.
    self.resource_group.Create()

    self.storage_account.Create()

    self.vnet.Create()

    self.subnet.Create()

    self.nsg.Create()
    self.nsg.AttachToSubnet()

  def Delete(self):
    """Deletes the network."""
    self.subnet.Delete()

    # Have to delete NSG after subnet or Azure will throw an error.
    self.nsg.Delete()

    self.vnet.Delete()

    self.storage_account.Delete()

    # If the benchmark includes multiple zones, this will be called
    # multiple times, but there will be no bad effects from multiple
    # deletes.
    self.resource_group.Delete()

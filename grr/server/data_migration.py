#!/usr/bin/env python
"""Helper class to migrate data."""

from __future__ import division

import multiprocessing.pool
import sys
import threading

from grr.lib import rdfvalue
from grr.lib import type_info
from grr.lib import utils
from grr.lib.rdfvalues import client as rdf_client
from grr.lib.rdfvalues import objects as rdf_objects
from grr.server import aff4
from grr.server import data_store
from grr.server.aff4_objects import aff4_grr

_CLIENT_BATCH_SIZE = 50
_CLIENT_VERSION_THRESHOLD = rdfvalue.Duration("24h")
_PROGRESS_INTERVAL = rdfvalue.Duration("1s")


def Migrate(thread_count=300):
  """Migrates clients from the legacy storage to the relational database.

  Args:
    thread_count: A number of threads to execute thr migration with.
  """
  Migrator().Execute(thread_count)


class Migrator(object):
  """A simple worker class that uses thread pool to drive the migration."""

  def __init__(self):
    self._lock = threading.Lock()

    self._total_count = 0
    self._migrated_count = 0
    self._last_progress_time = None

  def Execute(self, thread_count):
    """Runs the migration procedure.

    Args:
      thread_count: A number of threads to execute the migration with.

    Raises:
      AssertionError: If not all clients have been migrated.
      ValueError: If the relational database backend is not available.
    """
    if not data_store.RelationalDBWriteEnabled():
      raise ValueError("No relational database available.")

    sys.stdout.write("Collecting clients...\n")
    client_urns = _GetClientUrns()

    sys.stdout.write("Clients to migrate: {}\n".format(len(client_urns)))
    sys.stdout.write("Threads to use: {}\n".format(thread_count))

    self._total_count = len(client_urns)
    self._migrated_count = 0

    batches = utils.Grouper(client_urns, _CLIENT_BATCH_SIZE)

    self._Progress()
    pool = multiprocessing.pool.ThreadPool(processes=thread_count)
    pool.map(self._MigrateBatch, list(batches))
    self._Progress()

    if self._migrated_count == self._total_count:
      message = "\nMigration has been finished (migrated {} clients).\n".format(
          self._migrated_count)
      sys.stdout.write(message)
    else:
      message = "Not all clients have been migrated ({}/{})".format(
          self._migrated_count, self._total_count)
      raise AssertionError(message)

  def _MigrateBatch(self, batch):
    for client in aff4.FACTORY.MultiOpen(batch, mode="r", age=aff4.ALL_TIMES):
      _WriteClient(client)

      with self._lock:
        self._migrated_count += 1

        delta = rdfvalue.RDFDatetime.Now() - self._last_progress_time
        if delta >= _PROGRESS_INTERVAL:
          self._Progress()

  def _Progress(self):
    fraction = self._migrated_count / self._total_count
    message = "\rMigrating clients... {:>9}/{} ({:.2%})".format(
        self._migrated_count, self._total_count, fraction)
    sys.stdout.write(message)
    sys.stdout.flush()

    self._last_progress_time = rdfvalue.RDFDatetime.Now()


def _WriteClient(client):
  """Store the AFF4 client in the relational database."""
  _WriteClientMetadata(client)
  _WriteClientHistory(client)
  _WriteClientLabels(client)


def _WriteClientMetadata(client):
  """Store the AFF4 client metadata in the relational database."""
  client_ip = client.Get(client.Schema.CLIENT_IP)
  if client_ip:
    last_ip = rdf_client.NetworkAddress(
        human_readable_address=utils.SmartStr(client_ip))
  else:
    last_ip = None

  data_store.REL_DB.WriteClientMetadata(
      client.urn.Basename(),
      certificate=client.Get(client.Schema.CERT),
      fleetspeak_enabled=client.Get(client.Schema.FLEETSPEAK_ENABLED) or False,
      last_ping=client.Get(client.Schema.PING),
      last_clock=client.Get(client.Schema.CLOCK),
      last_ip=last_ip,
      last_foreman=client.Get(client.Schema.LAST_FOREMAN_TIME),
      first_seen=client.Get(client.Schema.FIRST_SEEN))


def _WriteClientHistory(client):
  """Store versions of the AFF4 client in the relational database."""
  snapshots = list()

  for version in _GetClientVersions(client):
    clone_attrs = {}
    for key, values in client.synced_attributes.iteritems():
      clone_attrs[key] = [value for value in values if value.age <= version.age]

    synced_client = aff4_grr.VFSGRRClient(
        client.urn, clone=clone_attrs, age=(0, version.age))

    client_snapshot = ConvertVFSGRRClient(synced_client)
    client_snapshot.timestamp = version.age
    snapshots.append(client_snapshot)

  data_store.REL_DB.WriteClientSnapshotHistory(snapshots)


def _WriteClientLabels(client):
  labels = dict()
  for label in client.Get(client.Schema.LABELS) or []:
    labels.setdefault(label.owner, []).append(label.name)

  for owner, names in labels.iteritems():
    data_store.REL_DB.AddClientLabels(client.urn.Basename(), owner, names)


def _GetClientUrns():
  """Returns a set of client URNs available in the data store."""
  result = set()

  for urn in aff4.FACTORY.ListChildren("aff4:/"):
    try:
      client_urn = rdf_client.ClientURN(urn)
    except type_info.TypeValueError:
      continue

    result.add(client_urn)

  return result


def _GetClientVersions(client):
  """Obtains a list of versions for the given client."""
  versions = []
  for typ in client.GetValuesForAttribute(client.Schema.TYPE):
    if not versions or versions[-1].age - typ.age > _CLIENT_VERSION_THRESHOLD:
      versions.append(typ)
  return versions


def ConvertVFSGRRClient(client):
  """Converts from `VFSGRRClient` to `rdfvalues.objects.ClientSnapshot`."""
  snapshot = rdf_objects.ClientSnapshot(client_id=client.urn.Basename())

  snapshot.filesystems = client.Get(client.Schema.FILESYSTEM)
  snapshot.hostname = client.Get(client.Schema.HOSTNAME)
  snapshot.fqdn = client.Get(client.Schema.FQDN)
  snapshot.system = client.Get(client.Schema.SYSTEM)
  snapshot.os_release = client.Get(client.Schema.OS_RELEASE)
  snapshot.os_version = utils.SmartStr(client.Get(client.Schema.OS_VERSION))
  snapshot.arch = client.Get(client.Schema.ARCH)
  snapshot.install_time = client.Get(client.Schema.INSTALL_DATE)
  snapshot.knowledge_base = client.Get(client.Schema.KNOWLEDGE_BASE)
  snapshot.startup_info.boot_time = client.Get(client.Schema.LAST_BOOT_TIME)
  snapshot.startup_info.client_info = client.Get(client.Schema.CLIENT_INFO)

  conf = client.Get(client.Schema.GRR_CONFIGURATION) or []
  for key in conf or []:
    snapshot.grr_configuration.Append(key=key, value=utils.SmartStr(conf[key]))

  lib = client.Get(client.Schema.LIBRARY_VERSIONS) or []
  for key in lib or []:
    snapshot.library_versions.Append(key=key, value=utils.SmartStr(lib[key]))

  snapshot.kernel = client.Get(client.Schema.KERNEL)
  snapshot.volumes = client.Get(client.Schema.VOLUMES)
  snapshot.interfaces = client.Get(client.Schema.INTERFACES)
  snapshot.hardware_info = client.Get(client.Schema.HARDWARE_INFO)
  snapshot.memory_size = client.Get(client.Schema.MEMORY_SIZE)
  snapshot.cloud_instance = client.Get(client.Schema.CLOUD_INSTANCE)

  return snapshot

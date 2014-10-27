#!/usr/bin/env python
"""Cron management classes."""


import threading
import time

import logging

from grr.lib import access_control
from grr.lib import aff4
from grr.lib import config_lib
from grr.lib import data_store
from grr.lib import flow
from grr.lib import master
from grr.lib import rdfvalue
from grr.lib import registry
from grr.lib import stats
from grr.lib import utils

from grr.proto import flows_pb2


config_lib.DEFINE_list("Cron.enabled_system_jobs", [],
                       "List of system cron jobs that will be "
                       "automatically scheduled on worker startup. "
                       "If cron jobs from this list were disabled "
                       "before, they will be enabled on worker "
                       "startup. Vice versa, if they were enabled "
                       "but are not specified in the list, they "
                       "will be disabled.")


class Error(Exception):
  pass


class CronSpec(rdfvalue.Duration):
  data_store_type = "string"

  def SerializeToDataStore(self):
    return self.SerializeToString()

  def ParseFromDataStore(self, value):
    return self.ParseFromString(value)


class CreateCronJobFlowArgs(rdfvalue.RDFProtoStruct):
  protobuf = flows_pb2.CreateCronJobFlowArgs

  def GetFlowArgsClass(self):
    if self.flow_runner_args.flow_name:
      flow_cls = flow.GRRFlow.classes.get(self.flow_runner_args.flow_name)
      if flow_cls is None:
        raise ValueError("Flow '%s' not known by this implementation." %
                         self.flow_runner_args.flow_name)

      # The required protobuf for this class is in args_type.
      return flow_cls.args_type


class CronManager(object):
  """CronManager is used to schedule/terminate cron jobs."""

  CRON_JOBS_PATH = rdfvalue.RDFURN("aff4:/cron")

  def ScheduleFlow(self, cron_args=None,
                   job_name=None, token=None, disabled=False):
    """Creates a cron job that runs given flow with a given frequency.

    Args:
      cron_args: A protobuf of type CreateCronJobFlowArgs.

      job_name: Use this job_name instead of an autogenerated unique name (used
                for system cron jobs - we want them to have well-defined
                persistent name).

      token: Security token used for data store access.

      disabled: If True, the job object will be created, but will be disabled.

    Returns:
      URN of the cron job created.
    """
    if not job_name:
      uid = utils.PRNG.GetUShort()
      job_name = "%s_%s" % (cron_args.flow_runner_args.flow_name, uid)

    cron_job_urn = self.CRON_JOBS_PATH.Add(job_name)
    cron_job = aff4.FACTORY.Create(cron_job_urn, aff4_type="CronJob", mode="rw",
                                   token=token, force_new_version=False)

    if cron_args != cron_job.Get(cron_job.Schema.CRON_ARGS):
      cron_job.Set(cron_job.Schema.CRON_ARGS(cron_args))

    if disabled != cron_job.Get(cron_job.Schema.DISABLED):
      cron_job.Set(cron_job.Schema.DISABLED(disabled))

    cron_job.Close()

    return cron_job_urn

  def ListJobs(self, token=None):
    """Returns a generator of URNs of all currently running cron jobs."""
    return aff4.FACTORY.Open(self.CRON_JOBS_PATH, token=token).ListChildren()

  def EnableJob(self, job_urn, token=None):
    """Enable cron job with the given URN."""
    cron_job = aff4.FACTORY.Open(job_urn, mode="rw", aff4_type="CronJob",
                                 token=token)
    cron_job.Set(cron_job.Schema.DISABLED(0))
    cron_job.Close()

  def DisableJob(self, job_urn, token=None):
    """Disable cron job with the given URN."""
    cron_job = aff4.FACTORY.Open(job_urn, mode="rw", aff4_type="CronJob",
                                 token=token)
    cron_job.Set(cron_job.Schema.DISABLED(1))
    cron_job.Close()

  def DeleteJob(self, job_urn, token=None):
    """Deletes cron job with the given URN."""
    aff4.FACTORY.Delete(job_urn, token=token)

  def RunOnce(self, token=None, force=False, urns=None):
    """Tries to lock and run cron jobs.

    Args:
      token: security token
      force: If True, force a run
      urns: List of URNs to run.  If unset, run them all
    """
    urns = urns or self.ListJobs(token=token)
    for cron_job_urn in urns:
      try:

        with aff4.FACTORY.OpenWithLock(
            cron_job_urn, blocking=False, token=token,
            lease_time=600) as cron_job:
          try:
            logging.info("Running cron job: %s", cron_job.urn)
            cron_job.Run(force=force)
          except Exception as e:  # pylint: disable=broad-except
            logging.exception("Error processing cron job %s: %s",
                              cron_job.urn, e)
            stats.STATS.IncrementCounter("cron_internal_error")

      except aff4.LockError:
        pass


CRON_MANAGER = CronManager()


class SystemCronFlow(flow.GRRFlow):
  """SystemCronFlows are scheduled automatically on workers startup."""

  frequency = rdfvalue.Duration("1d")
  lifetime = rdfvalue.Duration("20h")

  __abstract = True  # pylint: disable=g-bad-name

  def WriteState(self):
    if "w" in self.mode:
      # For normal flows it's a bug to write an empty state, here it's ok.
      self.Set(self.Schema.FLOW_STATE(self.state))


class StateReadError(Error):
  pass


class StateWriteError(Error):
  pass


class StatefulSystemCronFlow(SystemCronFlow):
  """SystemCronFlow that keeps a permanent state between iterations."""

  __abstract = True

  @property
  def cron_job_urn(self):
    return CRON_MANAGER.CRON_JOBS_PATH.Add(self.__class__.__name__)

  def ReadCronState(self):
    try:
      cron_job = aff4.FACTORY.Open(self.cron_job_urn, aff4_type="CronJob",
                                   token=self.token)
      return cron_job.Get(cron_job.Schema.STATE, default=rdfvalue.FlowState())
    except aff4.InstantiationError as e:
      raise StateReadError(e)

  def WriteCronState(self, state):
    try:
      with aff4.FACTORY.OpenWithLock(self.cron_job_urn, aff4_type="CronJob",
                                     token=self.token) as cron_job:
        cron_job.Set(cron_job.Schema.STATE(state))
    except aff4.InstantiationError as e:
      raise StateWriteError(e)


def ScheduleSystemCronFlows(token=None):
  """Schedule all the SystemCronFlows found."""

  for name in config_lib.CONFIG["Cron.enabled_system_jobs"]:
    try:
      cls = flow.GRRFlow.classes[name]
    except KeyError:
      raise KeyError("No such flow: %s." % name)

    if not aff4.issubclass(cls, SystemCronFlow):
      raise ValueError("Enabled system cron job name doesn't correspond to "
                       "a flow inherited from SystemCronFlow: %s" % name)

  for name, cls in flow.GRRFlow.classes.items():
    if aff4.issubclass(cls, SystemCronFlow):

      cron_args = CreateCronJobFlowArgs(periodicity=cls.frequency)
      cron_args.flow_runner_args.flow_name = name
      cron_args.lifetime = cls.lifetime

      disabled = name not in config_lib.CONFIG["Cron.enabled_system_jobs"]
      CRON_MANAGER.ScheduleFlow(cron_args=cron_args,
                                job_name=name, token=token,
                                disabled=disabled)


class CronWorker(object):
  """CronWorker runs a thread that periodically executes cron jobs."""

  def __init__(self, thread_name="grr_cron", sleep=60*5):
    self.thread_name = thread_name
    self.sleep = sleep

    self.token = access_control.ACLToken(
        username="GRRCron", reason="Implied.").SetUID()

  def _RunLoop(self):
    ScheduleSystemCronFlows(token=self.token)

    while True:
      if not master.MASTER_WATCHER.IsMaster():
        time.sleep(self.sleep)
        continue
      try:
        CRON_MANAGER.RunOnce(token=self.token)
      except Exception as e:  # pylint: disable=broad-except
        logging.error("CronWorker uncaught exception: %s", e)

      time.sleep(self.sleep)

  def Run(self):
    """Runs a working thread and waits for it to finish."""
    self.RunAsync().join()

  def RunAsync(self):
    """Runs a working thread and returns immediately."""
    self.running_thread = threading.Thread(name=self.thread_name,
                                           target=self._RunLoop)
    self.running_thread.daemon = True
    self.running_thread.start()
    return self.running_thread


class ManageCronJobFlowArgs(rdfvalue.RDFProtoStruct):
  protobuf = flows_pb2.ManageCronJobFlowArgs


class ManageCronJobFlow(flow.GRRFlow):
  """Manage an already created cron job."""
  # This flow can run on any client without ACL enforcement (an SUID flow).
  ACL_ENFORCED = False

  args_type = ManageCronJobFlowArgs

  @flow.StateHandler()
  def Start(self):
    data_store.DB.security_manager.CheckCronJobAccess(
        self.token.RealUID(), self.state.args.urn)

    if self.state.args.action == self.args_type.Action.DISABLE:
      CRON_MANAGER.DisableJob(self.state.args.urn, token=self.token)
    elif self.state.args.action == self.args_type.Action.ENABLE:
      CRON_MANAGER.EnableJob(self.state.args.urn, token=self.token)
    elif self.state.args.action == self.args_type.Action.DELETE:
      CRON_MANAGER.DeleteJob(self.state.args.urn, token=self.token)
    elif self.state.args.action == self.args_type.Action.RUN:
      CRON_MANAGER.RunOnce(urns=[self.state.args.urn], token=self.token,
                           force=True)


class CreateCronJobFlow(flow.GRRFlow):
  """Create a new cron job."""
  # This flow can run on any client without ACL enforcement (an SUID flow).
  ACL_ENFORCED = False

  args_type = CreateCronJobFlowArgs

  @flow.StateHandler()
  def Start(self):
    # Anyone can create a cron job but they need to get approval to start it.
    CRON_MANAGER.ScheduleFlow(cron_args=self.state.args, disabled=True,
                              token=self.token)


class CronJob(aff4.AFF4Volume):
  """AFF4 object corresponding to cron jobs."""

  class SchemaCls(aff4.AFF4Volume.SchemaCls):
    """Schema for CronJob AFF4 object."""
    CRON_ARGS = aff4.Attribute("aff4:cron/args", rdfvalue.CreateCronJobFlowArgs,
                               "This cron jobs' arguments.")

    DISABLED = aff4.Attribute(
        "aff4:cron/disabled", rdfvalue.RDFBool,
        "If True, don't run this job.", versioned=False)

    CURRENT_FLOW_URN = aff4.Attribute(
        "aff4:cron/current_flow_urn", rdfvalue.RDFURN,
        "URN of the currently running flow corresponding to this cron job.",
        versioned=False, lock_protected=True)

    LAST_RUN_TIME = aff4.Attribute(
        "aff4:cron/last_run", rdfvalue.RDFDatetime,
        "The last time this cron job ran.", "last_run",
        versioned=False, lock_protected=True)

    LAST_RUN_STATUS = aff4.Attribute(
        "aff4:cron/last_run_status", rdfvalue.CronJobRunStatus,
        "Result of the last flow", lock_protected=True,
        creates_new_object_version=False)

    STATE = aff4.Attribute(
        "aff4:cron/state", rdfvalue.FlowState,
        "Cron flow state that is kept between iterations", lock_protected=True,
        versioned=False)

  def IsRunning(self):
    """Returns True if there's a currently running iteration of this job."""
    current_urn = self.Get(self.Schema.CURRENT_FLOW_URN)
    if current_urn:
      current_flow = aff4.FACTORY.Open(urn=current_urn,
                                       token=self.token, mode="r")
      runner = current_flow.GetRunner()
      return runner.context.state == rdfvalue.Flow.State.RUNNING
    return False

  def DueToRun(self):
    """Called periodically by the cron daemon, if True Run() will be called.

    Returns:
        True if it is time to run based on the specified frequency.
    """
    if self.Get(self.Schema.DISABLED):
      return False

    cron_args = self.Get(self.Schema.CRON_ARGS)
    last_run_time = self.Get(self.Schema.LAST_RUN_TIME)
    now = rdfvalue.RDFDatetime().Now()

    # Its time to run.
    if (last_run_time is None or
        now > cron_args.periodicity.Expiry(last_run_time)):

      # Do we allow overruns?
      if cron_args.allow_overruns:
        return True

      # No currently executing job - lets go.
      if self.Get(self.Schema.CURRENT_FLOW_URN) is None:
        return True

    return False

  def StopCurrentRun(self, reason="Cron lifetime exceeded.", force=True):
    current_flow_urn = self.Get(self.Schema.CURRENT_FLOW_URN)
    if current_flow_urn:
      flow.GRRFlow.TerminateFlow(current_flow_urn, reason=reason, force=force,
                                 token=self.token)
      self.Set(self.Schema.LAST_RUN_STATUS,
               rdfvalue.CronJobRunStatus(
                   status=rdfvalue.CronJobRunStatus.Status.TIMEOUT))
      self.DeleteAttribute(self.Schema.CURRENT_FLOW_URN)
      self.Flush()

  def KillOldFlows(self):
    """Disable cron flow if it has exceeded CRON_ARGS.lifetime.

    Returns:
      bool: True if the flow is was killed, False if it is still alive
    """
    if self.IsRunning():
      start_time = self.Get(self.Schema.LAST_RUN_TIME)
      lifetime = self.Get(self.Schema.CRON_ARGS).lifetime
      elapsed = time.time() - start_time.AsSecondsFromEpoch()

      if lifetime and elapsed > lifetime.seconds:
        self.StopCurrentRun()
        stats.STATS.IncrementCounter("cron_job_timeout",
                                     fields=[self.urn.Basename()])
        stats.STATS.RecordEvent("cron_job_latency", elapsed,
                                fields=[self.urn.Basename()])
        return True

    return False

  def Run(self, force=False):
    """Do the actual work of the Cron. Will first check if DueToRun is True.

    CronJob object must be locked (i.e. opened via OpenWithLock) for Run() to be
    called.

    Args:
      force: If True, the job will run no matter what (i.e. even if DueToRun()
             returns False).

    Raises:
      LockError: if the object is not locked.
    """
    if not self.locked:
      raise aff4.LockError("CronJob must be locked for Run() to be called.")

    if self.KillOldFlows():
      return

    # If currently running flow has finished, update our state.
    current_flow_urn = self.Get(self.Schema.CURRENT_FLOW_URN)
    if current_flow_urn:
      current_flow = aff4.FACTORY.Open(current_flow_urn, token=self.token)
      runner = current_flow.GetRunner()
      if not runner.IsRunning():
        if runner.context.state == rdfvalue.Flow.State.ERROR:
          self.Set(self.Schema.LAST_RUN_STATUS,
                   rdfvalue.CronJobRunStatus(
                       status=rdfvalue.CronJobRunStatus.Status.ERROR))
          stats.STATS.IncrementCounter("cron_job_failure",
                                       fields=[self.urn.Basename()])
        else:
          self.Set(self.Schema.LAST_RUN_STATUS,
                   rdfvalue.CronJobRunStatus(
                       status=rdfvalue.CronJobRunStatus.Status.OK))

          start_time = self.Get(self.Schema.LAST_RUN_TIME)
          elapsed = time.time() - start_time.AsSecondsFromEpoch()
          stats.STATS.RecordEvent("cron_job_latency", elapsed,
                                  fields=[self.urn.Basename()])

        self.DeleteAttribute(self.Schema.CURRENT_FLOW_URN)
        self.Flush()

    if not force and not self.DueToRun():
      return

    cron_args = self.Get(self.Schema.CRON_ARGS)
    flow_urn = flow.GRRFlow.StartFlow(
        runner_args=cron_args.flow_runner_args,
        args=cron_args.flow_args, token=self.token, sync=False)

    self.Set(self.Schema.CURRENT_FLOW_URN, flow_urn)
    self.Set(self.Schema.LAST_RUN_TIME, rdfvalue.RDFDatetime().Now())
    self.Flush()

    flow_link = aff4.FACTORY.Create(self.urn.Add(flow_urn.Basename()),
                                    "AFF4Symlink", token=self.token)
    flow_link.Set(flow_link.Schema.SYMLINK_TARGET(flow_urn))
    flow_link.Close()


class CronHook(registry.InitHook):

  pre = ["AFF4InitHook", "MasterInit"]

  def RunOnce(self):
    """Main CronHook method."""
    stats.STATS.RegisterCounterMetric("cron_internal_error")
    stats.STATS.RegisterCounterMetric("cron_job_failure",
                                      fields=[("cron_job_name", str)])
    stats.STATS.RegisterCounterMetric("cron_job_timeout",
                                      fields=[("cron_job_name", str)])
    stats.STATS.RegisterEventMetric("cron_job_latency",
                                    fields=[("cron_job_name", str)])

    # Start the cron thread if configured to.
    if config_lib.CONFIG["Cron.active"]:

      self.cron_worker = CronWorker()
      self.cron_worker.RunAsync()
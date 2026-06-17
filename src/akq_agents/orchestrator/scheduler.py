from __future__ import annotations

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger


class QuantScheduler:
    def __init__(self, config, workflow):
        self.config = config
        self.workflow = workflow
        self.scheduler = BlockingScheduler(timezone=config.system.timezone)

    def register_jobs(self):
        sch = self.config.system.scheduler
        self.scheduler.add_job(
            self.workflow.run_once,
            IntervalTrigger(minutes=sch.factor_refresh_minutes),
            id="factor_pipeline",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.workflow.run_once_and_print,
            CronTrigger(hour=sch.daily_advice_hour, minute=sch.daily_advice_minute),
            id="daily_advice",
            replace_existing=True,
        )

    def start(self):
        self.register_jobs()
        self.scheduler.start()

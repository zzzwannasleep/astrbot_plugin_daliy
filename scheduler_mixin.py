from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime

from astrbot.api import logger

from daily_shared import SCHEDULER_RELOAD_POLL_SECONDS


class SchedulerMixin:
    def _start_scheduler(self):
        if self._scheduler_task and not self._scheduler_task.done():
            return
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def _stop_scheduler(self):
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._scheduler_task

    async def _scheduler_loop(self):
        while True:
            try:
                if not self._is_enabled():
                    await asyncio.sleep(60)
                    continue

                now = datetime.now(self._timezone())
                next_run = self._next_run_datetime(now)
                schedule_key = self._scheduler_config_key()
                logger.info("晨报插件下一次发送时间: %s", next_run.isoformat())
                reached_target = await self._sleep_until(next_run, schedule_key)
                if not reached_target:
                    logger.info("调度配置已更新，重新计算下一次发送时间。")
                    continue

                if not self._is_enabled():
                    continue

                today_key = datetime.now(self._timezone()).date().isoformat()
                last_delivery = await self.get_kv_data("last_delivery_date", "")
                if last_delivery == today_key:
                    continue

                success_count = await self._broadcast_daily_report(reason="schedule")
                if success_count > 0:
                    await self.put_kv_data("last_delivery_date", today_key)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("晨报定时任务异常: %s", exc)
                await asyncio.sleep(60)

    async def _sleep_until(self, target: datetime, expected_schedule_key: str) -> bool:
        while True:
            if self._scheduler_config_key() != expected_schedule_key:
                return False
            now = datetime.now(target.tzinfo)
            remaining = (target - now).total_seconds()
            if remaining <= 0:
                return True
            await asyncio.sleep(min(remaining, SCHEDULER_RELOAD_POLL_SECONDS))

    async def _maybe_send_startup_catchup(self):
        if not self.config.get("send_startup_catchup", False):
            return

        tz = self._timezone()
        now = datetime.now(tz)
        scheduled = now.replace(
            hour=self._delivery_hour(),
            minute=self._delivery_minute(),
            second=0,
            microsecond=0,
        )
        last_delivery = await self.get_kv_data("last_delivery_date", "")
        if now >= scheduled and last_delivery != now.date().isoformat():
            success_count = await self._broadcast_daily_report(reason="startup-catchup")
            if success_count > 0:
                await self.put_kv_data("last_delivery_date", now.date().isoformat())

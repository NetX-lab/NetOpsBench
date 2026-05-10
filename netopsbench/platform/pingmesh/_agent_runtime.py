"""Runtime loop helpers for Pingmesh agent."""

from __future__ import annotations

try:
    from ._agent_support import as_completed, logger, time
except ImportError:
    from _agent_support import as_completed, logger, time


class PingRuntimeMixin:
    def _probe_and_write(self, probe: dict) -> dict:
        try:
            result = self.udp_rtt_burst(probe["dst_ip"], probe.get("src_ip"))
            if self.enable_df_probe:
                df_result = self.udp_df_burst(probe["dst_ip"], probe.get("src_ip"))
                result["df_success"] = 1 if df_result.get("success") else 0
                result["df_loss_pct"] = float(df_result.get("loss_pct", 100.0))
                result["df_rtt_avg"] = float(df_result.get("rtt_avg", 0.0))
            else:
                result["df_success"] = 0
                result["df_loss_pct"] = 0.0
                result["df_rtt_avg"] = 0.0
            self.write_metrics(probe, result)
            if result["packets_lost"] > 0:
                self.write_drop_log(probe, result)
            return {"success": True, "probe": probe, "result": result}
        except Exception as e:
            logger.warning("Probe failed %s→%s: %s", probe["src_name"], probe["dst_name"], e)
            return {"success": False, "probe": probe, "error": str(e)}

    def run(self):
        logger.info("Starting parallel probe loop")
        logger.info("  Workers: %s", self.max_workers)
        if self.min_interval == self.max_interval:
            logger.info("  Fixed cycle interval: %ss", self.min_interval)
        else:
            logger.info("  Interval range: %s-%ss", self.min_interval, self.max_interval)
        while True:
            start = time.time()
            futures = {self.executor.submit(self._probe_and_write, probe): probe for probe in self.tasks}
            completed = 0
            failed = 0
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result["success"]:
                        completed += 1
                    else:
                        failed += 1
                except Exception as e:
                    probe = futures[future]
                    logger.warning("Unexpected probe error %s→%s: %s", probe["src_name"], probe["dst_name"], e)
                    failed += 1
            elapsed = time.time() - start
            target_interval = min(max(self.min_interval, elapsed * 1.5), self.max_interval)
            sleep_time = max(0, target_interval - elapsed)
            logger.info(
                "Cycle complete: %s succeeded, %s failed, %.1fs elapsed, sleeping %.1fs",
                completed,
                failed,
                elapsed,
                sleep_time,
            )
            time.sleep(sleep_time)

    def shutdown(self):
        logger.info("Shutting down agent, flushing remaining metrics...")
        self.shutdown_event.set()
        responder = getattr(self, "responder", None)
        if responder is not None:
            try:
                responder.stop()
            except Exception as exc:
                logger.warning("UDP responder shutdown error: %s", exc)
        self.write_queue.join()
        with self.batch_lock:
            if self.batch_buffer:
                self._write_to_influxdb(self.batch_buffer)
                self.batch_buffer = []
        if self.session:
            self.session.close()

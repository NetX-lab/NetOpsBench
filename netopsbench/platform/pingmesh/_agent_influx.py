"""Influx write helpers for Pingmesh agent."""

from __future__ import annotations

try:
    from ._agent_support import logger, queue, requests, time
except ImportError:
    from _agent_support import logger, queue, requests, time


class PingInfluxMixin:
    def _build_line_protocol(self, measurement: str, probe: dict, result: dict, timestamp: int = None) -> str:
        if timestamp is None:
            timestamp = int(time.time() * 1e9)
        _e = self._escape_tag_value
        if measurement == "pingmesh_drops":
            tags = [
                f"src_ip={_e(probe['src_ip'])}",
                f"dst_ip={_e(probe['dst_ip'])}",
                f"src_name={_e(probe['src_name'])}",
                f"dst_name={_e(probe['dst_name'])}",
                f"src_rack={_e(probe['src_rack'])}",
                f"dst_rack={_e(probe['dst_rack'])}",
                f"src_leaf={_e(probe['src_leaf'])}",
                f"dst_leaf={_e(probe['dst_leaf'])}",
            ]
            fields = [f"packets_lost={result['packets_lost']}i", f"loss_pct={result['loss_pct']}"]
        else:
            tags = [
                f"src_ip={_e(probe['src_ip'])}",
                f"dst_ip={_e(probe['dst_ip'])}",
                f"src_name={_e(probe['src_name'])}",
                f"dst_name={_e(probe['dst_name'])}",
                f"src_rack={_e(probe['src_rack'])}",
                f"dst_rack={_e(probe['dst_rack'])}",
                f"src_leaf={_e(probe['src_leaf'])}",
                f"dst_leaf={_e(probe['dst_leaf'])}",
                f"path_type={_e(probe['path_type'])}",
            ]
            fields = [
                f"rtt_min={result['rtt_min']}",
                f"rtt_avg={result['rtt_avg']}",
                f"rtt_max={result['rtt_max']}",
                f"rtt_p90={result['rtt_p90']}",
                f"rtt_p99={result['rtt_p99']}",
                f"packets_sent={result['packets_sent']}i",
                f"packets_lost={result['packets_lost']}i",
                f"packet_loss={result['loss_pct']}",
                f"df_success={int(result.get('df_success', 0))}i",
                f"df_loss_pct={result.get('df_loss_pct', 0.0)}",
                f"df_rtt_avg={result.get('df_rtt_avg', 0.0)}",
            ]
        if self.topology_id:
            tags.append(f"topology_id={_e(self.topology_id)}")
        return f"{measurement},{','.join(tags)} {','.join(fields)} {timestamp}"

    @staticmethod
    def _escape_tag_value(value: str) -> str:
        return str(value).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")

    def _write_to_influxdb(self, lines: list) -> bool:
        if not self.use_influxdb or not lines:
            return False
        url = f"{self.influxdb_url}/api/v2/write?org={self.influxdb_org}&bucket={self.influxdb_bucket}&precision=ns"
        data = "\n".join(lines)
        for attempt in range(self.max_retries):
            try:
                response = self.session.post(url, data=data, timeout=10)
                if response.status_code == 204:
                    return True
                if 400 <= response.status_code < 500:
                    logger.error("InfluxDB write failed (permanent): %s %s", response.status_code, response.text[:200])
                    return False
                if attempt < self.max_retries - 1:
                    backoff = self.retry_backoff_base * (2**attempt)
                    logger.warning(
                        "InfluxDB write failed (temporary): %s, retry %s/%s in %ss",
                        response.status_code,
                        attempt + 1,
                        self.max_retries,
                        backoff,
                    )
                    time.sleep(backoff)
            except requests.exceptions.RequestException as e:
                if attempt < self.max_retries - 1:
                    backoff = self.retry_backoff_base * (2**attempt)
                    logger.warning(
                        "InfluxDB write error (network): %s, retry %s/%s in %ss",
                        e,
                        attempt + 1,
                        self.max_retries,
                        backoff,
                    )
                    time.sleep(backoff)
                else:
                    logger.error("InfluxDB write failed after %s attempts: %s", self.max_retries, e)
                    return False
            except Exception as e:
                logger.error("InfluxDB write unexpected error: %s", e)
                return False
        return False

    def _batch_writer_worker(self):
        while not self.shutdown_event.is_set():
            try:
                item = None
                try:
                    item = self.write_queue.get(timeout=self.batch_timeout)
                except queue.Empty:
                    pass
                should_flush = False
                with self.batch_lock:
                    if item:
                        self.batch_buffer.append(item)
                    current_time = time.time()
                    time_since_flush = current_time - self.batch_last_flush
                    if len(self.batch_buffer) >= self.batch_size or (
                        time_since_flush >= self.batch_timeout and self.batch_buffer
                    ):
                        should_flush = True
                        lines_to_write = self.batch_buffer[:]
                        self.batch_buffer = []
                        self.batch_last_flush = current_time
                if should_flush and lines_to_write:
                    self._write_to_influxdb(lines_to_write)
                if item:
                    self.write_queue.task_done()
            except Exception as e:
                logger.error("Batch writer error: %s", e)

    def write_metrics(self, probe: dict, result: dict):
        if not self.use_influxdb:
            logger.warning("InfluxDB not available, metrics dropped")
            return
        timestamp = int(time.time() * 1e9)
        line = self._build_line_protocol("pingmesh", probe, result, timestamp)
        self.write_queue.put(line)

    def write_drop_log(self, probe: dict, result: dict):
        if not self.use_influxdb:
            return
        timestamp = int(time.time() * 1e9)
        line = self._build_line_protocol("pingmesh_drops", probe, result, timestamp)
        self.write_queue.put(line)

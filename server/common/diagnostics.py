"""Bounded, asynchronous metadata logs. Never serialize arbitrary worker payloads."""
import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import queue
import re
import threading
import time

EVENTS = {"worker_start", "sco_ready", "model_connected", "model_session_ready", "call_answered",
          "backend_response", "model_error", "audio_activity", "audio_stats", "session_end",
          "bridge_failure", "worker_exit", "call_state", "audio_stage", "operator_command", "operation_failure"}
NUMBERS = {"sequence", "elapsedMs", "durationMs", "inputBytes", "outputBytes", "modelQueuedBytes",
           "modelPeakQueuedBytes", "gatewayQueuedBytes", "backendEvents", "backendCreated",
           "backendCompleted", "backendFailed", "exitCode", "droppedLogs", "backendResponseIndex", "revision", "workerDroppedLogs", "frames", "meanMs", "maxMs", "bytes", "peakDepth", "audioSequence", "turnIndex", "workerElapsedMs"}
MODELS = {"resolvedVoiceModel", "resolvedBackendModel", "observedBackendModel"}
FAILURES = {"startup_timeout", "model_audio_queue_full", "model_audio_send_timeout", "model_audio_send",
            "invalid_capture_frame", "model_api", "invalid_model_event", "model_connection",
            "gateway_startup", "call_state", "lease_lost", "gateway_audio", "gateway_connection",
            "gateway_disconnected", "invalid_gateway_frame", "private_audio_queue_full",
            "private_audio_send_timeout", "private_audio_send"}
ERROR_CODES = FAILURES | {'audio_ready_timeout', 'audio_reader_timeout', 'invalid_audio_handshake',
    'audio_transport_timeout', 'audio_peer_closed', 'audio_backpressure', 'invalid_pcm_frame',
    'audio_not_ready', 'sco_closed', 'sco_accept_failed', 'audio_cleanup_failed', 'audio_busy',
    'phone_disconnected', 'native_transport_failed', 'hfp_query_timeout', 'hfp_query_failed',
    'hfp_control_unsynchronized', 'unclassified'}
ERROR_STAGES = {'reader_ready', 'sco_start', 'capture_transport', 'playback_handoff', 'control_transport',
    'audio_monitor', 'audio_cleanup', 'api_request', 'conversation_record', 'inbound_answer'}


def exception_fields(error, stage):
    """Exception identities only. Never include messages, repr or traceback payloads."""
    code = getattr(error, 'code', None)
    if not isinstance(code, str) or code not in ERROR_CODES:
        if isinstance(error, TimeoutError) and stage != 'api_request':
            code = 'audio_transport_timeout'
        elif isinstance(error, (asyncio.IncompleteReadError, ConnectionError)) and stage != 'api_request':
            code = 'audio_peer_closed'
        else:
            code = 'unclassified'
    result = {'event': 'operation_failure', 'errorStage': stage, 'errorCode': code,
              'errorType': type(error).__name__}
    if error.__cause__ is not None:
        result['causeType'] = type(error.__cause__).__name__
    return result


def sanitize(value):
    if not isinstance(value, dict) or not isinstance(value.get("event"), str) or value["event"] not in EVENTS:
        return None
    result = {"event": value["event"]}
    for key in ('errorType', 'causeType'):
        if isinstance(value.get(key), str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', value[key]):
            result[key] = value[key]
    if isinstance(value.get('errorCode'), str) and value['errorCode'] in ERROR_CODES:
        result['errorCode'] = value['errorCode']
    if isinstance(value.get('errorStage'), str) and value['errorStage'] in ERROR_STAGES:
        result['errorStage'] = value['errorStage']
    if isinstance(value.get('commandId'), str) and re.fullmatch(r'[a-f0-9]{32}', value['commandId']):
        result['commandId'] = value['commandId']
        result['clientEventId'] = 'operator_' + value['commandId']
    if isinstance(value.get('commandStatus'), str) and value['commandStatus'] in {'accepted', 'written', 'injected', 'rejected', 'unknown'}:
        result['commandStatus'] = value['commandStatus']
    if isinstance(value.get('commandReason'), str) and value['commandReason'] in {'command_limit', 'not_ready', 'command_too_large', 'model_write_failed', 'provider_rejected', 'ack_timeout', 'conversation_closed', 'worker_write_failed', 'worker_timeout'}:
        result['commandReason'] = value['commandReason']
    if value.get('clockDomain') in ('python_monotonic', 'node_performance'):
        result['clockDomain'] = value['clockDomain']
    if isinstance(value.get('stage'), str) and value['stage'] in {'capture_transport', 'control_transport', 'private_audio_send', 'playback_handoff', 'transport_failure',
            'monitor_drop', 'monitor_enqueue', 'monitor_send', 'monitor_failure', 'model_input_send',
            'model_connect', 'model_session_setup', 'input_queue_clear', 'playback_queue_clear', 'model_output_handoff', 'model_first_output', 'model_output_gap', 'hfp_capture_process',
            'hfp_capture_queue', 'hfp_playback_queue', 'hfp_underrun', 'hfp_host_send', 'interruption_observed', 'reader_ready'}:
        result['stage'] = value['stage']
    for key in ('state', 'lastKnownState'):
        if isinstance(value.get(key), str) and value[key] in {'unknown', 'dialing', 'alerting', 'active', 'held', 'incoming', 'waiting', 'ended'}:
            result[key] = value[key]
    for key in NUMBERS:
        item = value.get(key)
        if type(item) in (int, float) and -1 <= item <= 1e15:
            result[key] = item
    for key in MODELS:
        item = value.get(key)
        if isinstance(item, str) and re.fullmatch(r"gpt-[a-z0-9.-]{1,80}", item):
            result[key] = item
    models = value.get("observedBackendModels")
    if isinstance(models, list):
        result["observedBackendModels"] = [m for m in models[:32] if isinstance(m, str) and re.fullmatch(r"gpt-[a-z0-9.-]{1,80}", m)]
    if isinstance(value.get("responseEvent"), str) and value["responseEvent"] in {"response.created", "response.completed", "response.failed", "response.incomplete", "response.cancelled"}:
        result["responseEvent"] = value["responseEvent"]
    if isinstance(value.get("direction"), str) and value["direction"] in {"input", "output"}:
        result["direction"] = value["direction"]
    if isinstance(value.get("failureCode"), str) and value["failureCode"] in FAILURES:
        result["failureCode"] = value["failureCode"]
    for key in ("active", "deliveryGap", "finalized"):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    return result


class PrivateHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")

    def handleError(self, record):
        raise OSError("diagnostic_write_failed")  # Do not print arbitrary log records.


class Diagnostics:
    def __init__(self, root):
        self.root = root
        self.pending = queue.Queue(maxsize=256)
        self.dropped = 0
        self.closed = False
        self.failed = False
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="phone-metadata-log", daemon=True)
        self.thread.start()

    def write(self, conversation, value):
        record = sanitize(value)
        if not record or self.closed or not isinstance(conversation, str) or not re.fullmatch(r"[a-f0-9]{32}", conversation):
            return
        if self.failed:
            self.dropped += 1
            return
        record.update(conversationId=conversation, timestampMs=round(time.time() * 1000))
        record["droppedLogs"] = self.dropped
        try:
            self.pending.put_nowait(record)
        except queue.Full:
            self.dropped += 1  # Never wait or fail a phone call for logging.

    def run(self):
        handler = None
        try:
            directory = self.root / "logs"
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
            handler = PrivateHandler(directory / "calls.jsonl", maxBytes=2 * 1024 * 1024, backupCount=3)
            handler.setFormatter(logging.Formatter("%(message)s"))
            while not self.stop.is_set() or not self.pending.empty():
                try:
                    item = self.pending.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    handler.emit(logging.LogRecord("phone", logging.INFO, "", 0, json.dumps(item), (), None))
                except OSError:
                    self.dropped += 1
        except OSError:
            self.failed = True
            self.dropped += 1
        finally:
            if handler:
                handler.close()

    async def close(self):
        self.closed = True
        self.stop.set()
        await asyncio.to_thread(self.thread.join, 2)

# Changelog

All notable changes to this project are recorded in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Optional call recording. Set `PHONE_RECORD_CALLS=1` to save a private stereo WAV at `conversations/<id>/call.wav` (left: phone, right: AI). Off by default. A recording failure does not affect the call. After the call ends, the console can play the file.
- Bluetooth hands-free (HFP) gateway for a real phone, on a Mac (built-in controller) or on Linux / Raspberry Pi (BlueZ + PipeWire).
- AI voice agent for outbound calls, with a saved task per call and live operator instructions.
- Automatic answer of incoming calls after about 3 seconds, with a per-phone inbound prompt and a fixed guard prompt.
- Saved conversations (outbound and inbound) with full transcripts. Each record keeps the dialed number, or the caller number when the phone provides a numeric caller ID. The console shows that number. Logs, transcripts and the device call list still omit phone numbers. Call audio is not stored.
- Web console in English and Chinese: devices, pairing, calls, live transcript, listen-in and voice selection.
- One call line with recoverable per-call lifecycle.

# Changelog

All notable changes to this project are recorded in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Bluetooth hands-free (HFP) gateway for a real phone, on a Mac (built-in controller) or on Linux / Raspberry Pi (BlueZ + PipeWire).
- AI voice agent for outbound calls, with a saved task per call and live operator instructions.
- Automatic answer of incoming calls after about 3 seconds, with a per-phone inbound prompt and a fixed guard prompt.
- Saved conversations (outbound and inbound) with full transcripts. Phone numbers and call audio are not stored.
- Web console in English and Chinese: devices, pairing, calls, live transcript, listen-in and voice selection.
- One call line with recoverable per-call lifecycle.

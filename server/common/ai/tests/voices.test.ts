import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { validateVoice } from '../src/voices.js';

test('worker accepts the 13 bundled voices and rejects removed voices', () => {
  const manifest = JSON.parse(readFileSync(new URL('../../voice-samples/manifest.json', import.meta.url), 'utf8'));
  const voices = Object.keys(manifest.samples);
  assert.equal(voices.length, 13);
  assert.equal(validateVoice(), 'marin');
  for (const voice of voices) assert.equal(validateVoice(voice), voice);
  for (const voice of ['nuri', 'juni', 'flitz', 'brise', 'sillage', 'nira', 'harema', 'shida', 'noeul']) {
    assert.throws(() => validateVoice(voice), /invalid_voice/);
  }
});

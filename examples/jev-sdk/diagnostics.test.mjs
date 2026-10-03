import test from 'node:test';
import assert from 'node:assert/strict';
import { evaluateRefund } from './evaluate.mjs';

test('returns a safe HTTP failure without private response body', async () => {
  const fetch = async () => new Response(JSON.stringify({ error: { type: 'private_detail' } }), { status: 403 });
  await assert.rejects(() => evaluateRefund({ apiKey: 'test-only', fetch }), error => { assert.equal(error.statusCode, 403); assert.equal(error.cause, undefined); return true; });
});

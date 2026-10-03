import test from 'node:test';
import assert from 'node:assert/strict';
import { evaluateRefund } from './evaluate.mjs';

test('requires the server-side key', async () => { await assert.rejects(() => evaluateRefund({ apiKey: '' }), { code: 'missing_api_key' }); });

test('uses TypeSafe System One and parses a Noul answer', async () => {
  let request;
  const result = await evaluateRefund({ apiKey: 'test-only', fetch: async (input, init) => {
    request = { input: String(input), init };
    return new Response(JSON.stringify({ model: 'jev-1.13.0', answers: { refunded: { type: 'noul', noul: 0.99 } }, usage: { input_tokens: 1, output_tokens: 1 } }), { status: 200 });
  }});
  assert.equal(result.decision, true); assert.equal(result.answer.noul, 0.99);
  assert.equal(request.input, 'https://api.typesafe.ai/v1/systemone');
  assert.equal(request.init.headers.authorization, 'Bearer test-only');
  const body = JSON.parse(request.init.body); assert.equal(body.model, 'jev-latest'); assert.equal(body.questions.refunded.type, 'noul');
});

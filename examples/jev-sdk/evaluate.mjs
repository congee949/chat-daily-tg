import { loadEnvFile } from 'node:process';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

export async function evaluateRefund({ apiKey = process.env.TYPESAFE_API_KEY, baseURL = process.env.TYPESAFE_BASE_URL ?? 'https://api.typesafe.ai/v1', fetch = globalThis.fetch } = {}) {
  if (!apiKey?.trim()) throw Object.assign(new Error('Missing TypeSafe key'), { code: 'missing_api_key' });
  const endpoint = baseURL.replace(/\/$/, '') + '/systemone';
  if (endpoint !== 'https://api.typesafe.ai/v1/systemone') throw new Error('Unsupported TypeSafe endpoint');
  const response = await fetch(endpoint, {
    method: 'POST',
    headers: { authorization: 'Bearer ' + apiKey, 'content-type': 'application/json' },
    body: JSON.stringify({ model: 'jev-latest', state: 'The support agent issued a full refund to the customer.', questions: { refunded: { type: 'noul', instructions: 'Was a refund issued?' } } }),
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) throw Object.assign(new Error('Evaluation failed'), { statusCode: response.status });
  const result = await response.json();
  const answer = result.answers?.refunded;
  if (answer?.type !== 'noul' || typeof answer.noul !== 'number' || !Number.isFinite(answer.noul) || answer.noul < 0 || answer.noul > 1) throw new Error('Invalid Noul answer');
  return { model: result.model, answer, decision: answer.noul >= 0.5, usage: result.usage };
}

async function main() {
  try {
    if (!process.env.TYPESAFE_API_KEY?.trim()) {
      try { loadEnvFile(join(homedir(), 'chat-daily', '.env')); } catch (error) { if (error.code !== 'ENOENT') throw error; }
    }
    console.log(JSON.stringify({ status: 'ok', ...(await evaluateRefund()) }, null, 2));
  } catch (error) {
    console.error(JSON.stringify({ status: 'failed', code: error.code === 'missing_api_key' ? 'missing_api_key' : 'evaluation_failed', httpStatus: Number.isInteger(error.statusCode) ? error.statusCode : null }));
    process.exitCode = 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) await main();

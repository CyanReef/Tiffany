// Koishi's native Satori Session -> public Bot.dispatch -> core middleware.
// Official MockBot supplies the offline platform; no replacement dispatcher.
const path = require('node:path');
const http = require('node:http');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const options = JSON.parse(process.argv[2]);
const load = createRequire(path.join(options.node_env, 'package.json'));
const { App, Logger, h } = load('koishi');
const { MockBot } = load('@koishijs/plugin-mock');
Logger.levels.base = 1;

function percentile(values, fraction) {
  const sorted = [...values].sort((a, b) => a - b);
  const pos = (sorted.length - 1) * fraction;
  const low = Math.floor(pos), high = Math.ceil(pos);
  return (sorted[low] + (sorted[high] - sorted[low]) * (pos - low)) / 1000;
}

class Consumer {
  calls = 0; characters = 0; unmatched_calls = 0; service_times = [];
  constructor(url) {
    this.url = url;
    this.agent = new http.Agent({keepAlive: true, maxSockets: 16});
  }
  async accept(text) {
    assert.equal(text, 'hello');
    if (this.url) {
      await new Promise((resolve, reject) => {
        const request = http.get(this.url, {agent: this.agent}, response => {
          let body = '';
          response.setEncoding('utf8');
          response.on('data', chunk => {body += chunk;});
          response.on('error', reject);
          response.on('end', () => {
            try {
              assert.equal(response.statusCode, 200);
              assert.equal(body, 'hello');
              this.service_times.push(Number(response.headers['x-service-delay-ns']));
              resolve();
            } catch (error) {reject(error);}
          });
        });
        request.on('error', reject);
      });
    }
    this.calls++; this.characters += text.length;
  }
  reset() {this.calls = this.characters = this.unmatched_calls = 0; this.service_times = [];}
  close() {this.agent.destroy();}
}

async function measure(case_) {
  const consumer = new Consumer(case_.sessions === 16 ? options.io_url : null);
  const setupStart = process.hrtime.bigint();
  const app = new App();
  const botScope = app.plugin(MockBot, {selfId: '12345'});
  const pending = new Map();
  app.on('middleware', session => {
    const resolve = pending.get(session.id);
    if (resolve) {pending.delete(session.id); resolve();}
  });
  for (let index = 0; index < case_.matching; index++) {
    app.middleware(async (session, next) => {
      await consumer.accept(session.content);
      return next();
    });
  }
  for (let index = 0; index < case_.unmatched; index++) {
    app.on('friend-added', () => {consumer.unmatched_calls++;});
  }
  await app.start();
  const bot = app.bots[0];
  assert(bot instanceof MockBot);
  const setupSeconds = Number(process.hrtime.bigint() - setupStart) / 1e9;
  async function emit(index, sessionIndex) {
    // Fresh native protocol object and h.parse in Session preprocessing.
    const session = bot.session({
      type: 'message-created', platform: 'mock', selfId: '12345',
      timestamp: 1700000000000, user: {id: String(20000 + sessionIndex), name: 'benchmark'},
      channel: {id: `private:${20000 + sessionIndex}`, type: 1},
      message: {id: String(index), content: 'hello', elements: h.parse('hello')},
    });
    await new Promise(resolve => {pending.set(session.id, resolve); bot.dispatch(session);});
  }
  try {
    const sessions = case_.sessions || 1;
    for (let index = 0; index < options.warmup; index++) await emit(index, index % sessions);
    assert.equal(consumer.calls, options.warmup * case_.matching);
    assert.equal(consumer.unmatched_calls, 0);
    if (global.gc) global.gc();
    consumer.reset();
    const rssBefore = process.memoryUsage().rss;
    const cpuStart = process.cpuUsage(), start = process.hrtime.bigint();
    const latencies = [];
    async function producer(sessionIndex) {
      for (let index = sessionIndex; index < options.events; index += sessions) {
        const eventStart = process.hrtime.bigint();
        await emit(index, sessionIndex);
        latencies.push(Number(process.hrtime.bigint() - eventStart));
      }
    }
    await Promise.all(Array.from({length: sessions}, (_, index) => producer(index)));
    const elapsed = Number(process.hrtime.bigint() - start) / 1e9;
    const cpu = process.cpuUsage(cpuStart);
    const expected = options.events * case_.matching;
    assert.equal(consumer.calls, expected);
    assert.equal(consumer.characters, expected * 5);
    assert.equal(consumer.unmatched_calls, 0);
    assert.equal(pending.size, 0);
    const result = {...case_, events: options.events, elapsed_seconds: elapsed,
      events_per_second: options.events / elapsed, cpu_seconds: (cpu.user + cpu.system) / 1e6,
      latency_p50_us: percentile(latencies, .5), latency_p95_us: percentile(latencies, .95),
      latency_p99_us: percentile(latencies, .99), latency_max_us: Math.max(...latencies) / 1000,
      rss_before_bytes: rssBefore, rss_after_bytes: process.memoryUsage().rss,
      setup_seconds: setupSeconds, verified_handler_calls: consumer.calls,
      verified_characters: consumer.characters, verified_unmatched_calls: consumer.unmatched_calls};
    if (consumer.service_times.length) {
      result.service_delay_p50_us = percentile(consumer.service_times, .5);
      result.service_delay_p95_us = percentile(consumer.service_times, .95);
      result.verified_http_calls = consumer.service_times.length;
    }
    return result;
  } finally {consumer.close(); await botScope.dispose(); await app.stop();}
}

(async () => {
  const result = {framework: 'koishi', round: options.round, cases: [],
    runtime: process.version, event_loop: 'libuv', gc_enabled: true,
    packages: Object.fromEntries(['koishi', '@koishijs/core', '@koishijs/plugin-mock', '@satorijs/core'].map(name =>
      [name, JSON.parse(fs.readFileSync(path.join(options.node_env, 'node_modules', name, 'package.json'), 'utf8')).version]))};
  for (const case_ of options.cases) result.cases.push(await measure(case_));
  console.log(JSON.stringify(result));
})().catch(error => {console.error(error); process.exitCode = 1;});

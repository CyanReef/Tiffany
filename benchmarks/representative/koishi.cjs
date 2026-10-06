// Native Satori -> official MockBot -> Koishi middleware, no dispatcher shim.
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
const send = value => console.log(JSON.stringify(value));
const now = () => Number(process.hrtime.bigint());
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

function percentile(values, fraction) {
  const sorted = [...values].sort((a, b) => a - b);
  const pos = (sorted.length - 1) * fraction;
  const low = Math.floor(pos), high = Math.ceil(pos);
  return (sorted[low] + (sorted[high] - sorted[low]) * (pos - low)) / 1000;
}

class Consumer {
  constructor(case_) {
    this.case = case_;
    this.parts = case_.text_bytes ? ['hello' + 'x'.repeat(case_.text_bytes - 5)]
      : Array(case_.segments || 1).fill('hello');
    this.expected = this.parts.join('');
    this.agent = new http.Agent({keepAlive: true, maxSockets: 64});
    this.gate = new Promise(resolve => {this.release = resolve;});
    this.reset();
  }
  reset() {
    this.calls = this.characters = this.unmatched_calls = this.predicate_calls = 0;
    this.service_times = []; this.started = []; this.finished = [];
    this.active = this.peak_active = this.session_overlap = 0;
    this.sessions_active = new Map();
  }
  async accept(text, index, session) {
    assert.equal(text, this.expected);
    if (this.case.burst) {
      this.started.push(index); this.active++;
      this.peak_active = Math.max(this.peak_active, this.active);
      const n = (this.sessions_active.get(session) || 0) + 1;
      this.sessions_active.set(session, n);
      this.session_overlap = Math.max(this.session_overlap, n);
      await this.gate;
      this.active--; this.sessions_active.set(session, this.sessions_active.get(session) - 1);
      this.finished.push(index);
    }
    if (this.case.http) {
      await new Promise((resolve, reject) => {
        const request = http.get(options.io_url, {agent: this.agent}, response => {
          let body = '';
          response.setEncoding('utf8');
          response.on('data', chunk => {body += chunk;});
          response.on('error', reject);
          response.on('end', () => {
            try {
              assert.equal(response.statusCode, 200); assert.equal(body, 'hello');
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
  verify(events) {
    assert.equal(this.calls, events * this.case.matching);
    assert.equal(this.characters, this.calls * this.expected.length);
    assert.equal(this.unmatched_calls, 0);
    assert.equal(this.predicate_calls, events * (this.case.predicates || 0));
    assert.equal(this.service_times.length, this.case.http ? events : 0);
    return {verified_handler_calls: this.calls, verified_characters: this.characters,
      verified_unmatched_calls: this.unmatched_calls, verified_predicate_calls: this.predicate_calls,
      verified_http_calls: this.service_times.length};
  }
}

async function measure(case_, first) {
  send({phase: 'case_start', case: case_.name});
  const consumer = new Consumer(case_), setupStart = now();
  const app = new App();
  const botScope = app.plugin(MockBot, {selfId: '12345'});
  const pending = new Map();
  app.on('middleware', session => {
    const resolve = pending.get(session.id);
    if (resolve) {pending.delete(session.id); resolve();}
  });
  for (let index = 0; index < case_.matching; index++) {
    app.middleware(async (session, next) => {
      let text;
      for (let read = 0; read < (case_.reads || 1); read++) text = session.content;
      if (case_.burst) await consumer.accept(text, Number(session.messageId), Number(session.userId) - 20000);
      else await consumer.accept(text);
      return next();
    });
  }
  for (let index = 0; index < (case_.unmatched || 0); index++) {
    app.on('friend-added', () => {consumer.unmatched_calls++;});
  }
  for (let index = 0; index < (case_.predicates || 0); index++) {
    app.intersect(() => {consumer.predicate_calls++; return false;})
      .middleware(async (session, next) => {consumer.unmatched_calls++; return next();});
  }
  await app.start();
  const bot = app.bots[0]; assert(bot instanceof MockBot);
  const setupSeconds = (now() - setupStart) / 1e9;
  if (first) send({phase: 'ready'});
  async function emit(index, sessionIndex) {
    const session = bot.session({
      type: 'message-created', platform: 'mock', selfId: '12345', timestamp: 1700000000000,
      user: {id: String(20000 + sessionIndex), name: 'benchmark'},
      channel: {id: `private:${20000 + sessionIndex}`, type: 1},
      message: {id: String(index), content: consumer.expected, elements: consumer.parts.flatMap(part => h.parse(part))},
    });
    await new Promise(resolve => {pending.set(session.id, resolve); bot.dispatch(session);});
  }
  try {
    let result;
    const sessions = case_.sessions || 1;
    if (case_.burst) {
      const tasks = Array.from({length: case_.burst}, (_, index) => emit(index, index % sessions));
      let previous = -1, stableSince = now(), deadline = now() + 10e9;
      while (now() < deadline) {
        await wait(10);
        if (consumer.started.length !== previous) {previous = consumer.started.length; stableSince = now();}
        if (previous > 0 && now() - stableSince >= .1e9) break;
      }
      assert(now() < deadline, 'burst did not settle');
      const blocked = consumer.started.length;
      consumer.release(); await Promise.all(tasks);
      assert.equal(new Set(consumer.finished).size, case_.burst);
      assert.equal(consumer.active, 0);
      const fifo = Array.from({length: sessions}, (_, session) => {
        const indices = consumer.finished.filter(index => index % sessions === session);
        return indices.every((index, pos) => !pos || index > indices[pos - 1]);
      }).every(Boolean);
      result = {...case_, submitted: case_.burst, completed: consumer.calls, rejected: 0,
        blocked_handlers: blocked, peak_active_handlers: consumer.peak_active,
        max_same_session_overlap: consumer.session_overlap, observed_completion_fifo: fifo,
        ...consumer.verify(case_.burst)};
    } else {
      for (let index = 0; index < options.warmup; index++) await emit(index, index % sessions);
      consumer.verify(options.warmup);
      if (global.gc) global.gc();
      consumer.reset();
      const rss = process.memoryUsage().rss, latencies = [];
      send({phase: 'timing', case: case_.name});
      const cpuStart = process.cpuUsage(), start = now();
      async function producer(session) {
        for (let index = session; index < options.events; index += sessions) {
          const eventStart = now(); await emit(index, session);
          latencies.push(now() - eventStart);
        }
      }
      await Promise.all(Array.from({length: sessions}, (_, index) => producer(index)));
      const elapsed = (now() - start) / 1e9, cpu = process.cpuUsage(cpuStart);
      const cpuSeconds = (cpu.user + cpu.system) / 1e6;
      result = {...case_, events: options.events, elapsed_seconds: elapsed, events_per_second: options.events / elapsed,
        cpu_seconds: cpuSeconds, cpu_us_per_event: cpuSeconds / options.events * 1e6,
        latency_p50_us: percentile(latencies, .5), latency_p95_us: percentile(latencies, .95),
        latency_p99_us: percentile(latencies, .99), latency_max_us: Math.max(...latencies) / 1000,
        rss_before_bytes: rss, ...consumer.verify(options.events)};
      if (consumer.service_times.length) {
        assert(Math.min(...consumer.service_times) >= 5e6);
        Object.assign(result, {service_delay_p50_us: percentile(consumer.service_times, .5),
          service_delay_p95_us: percentile(consumer.service_times, .95),
          service_delay_min_us: Math.min(...consumer.service_times) / 1000});
      }
    }
    assert.equal(pending.size, 0);
    return {...result, rss_after_bytes: process.memoryUsage().rss, setup_seconds: setupSeconds};
  } finally {consumer.agent.destroy(); await botScope.dispose(); await app.stop();}
}

(async () => {
  for (const [index, case_] of options.cases.entries()) {
    send({phase: 'case_result', result: await measure(case_, index === 0)});
  }
  send({phase: 'complete', runtime: process.version, event_loop: 'libuv', gc_enabled: true,
    packages: Object.fromEntries(['koishi', '@koishijs/core', '@koishijs/plugin-mock', '@satorijs/core'].map(name =>
      [name, JSON.parse(fs.readFileSync(path.join(options.node_env, 'node_modules', name, 'package.json'), 'utf8')).version]))});
})().catch(error => {console.error(error); process.exitCode = 1;});

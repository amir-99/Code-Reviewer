import {test} from 'node:test';
import assert from 'node:assert/strict';
import {events} from './sse.js';

test('decodes split UTF-8, CRLF, heartbeat and reconnect ids', async () => {
  const bytes = new TextEncoder().encode('retry: 2000\r\n\r\n: heartbeat\r\n\r\nid: 42\r\nevent: activity\r\ndata: {"name":"réview"}\r\n\r\nevent: complete\ndata: {}\n\n');
  const body = new ReadableStream({start(c) { for (const b of bytes) c.enqueue(Uint8Array.of(b)); c.close(); }});
  const result = []; for await (const event of events(body)) result.push(event);
  assert.deepEqual(result, [{event:'activity', id:'42', data:{name:'réview'}}, {event:'complete',id:undefined,data:{}}]);
});

test('cancels reader when terminal event ends consumption', async () => {
  let cancelled = false;
  const body = new ReadableStream({start(c) {c.enqueue(new TextEncoder().encode('event: complete\ndata: {}\n\n'));}, cancel() {cancelled = true;}});
  for await (const event of events(body)) {assert.equal(event.event, 'complete'); break;}
  assert.equal(cancelled, true);
});

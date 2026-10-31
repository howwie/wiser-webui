// Run with: node --test tests/schedule.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

import { nextChange } from "../app/static/schedule.mjs";

const week = (...entries) => Array.from({ length: 7 }, () => entries);
const WEDNESDAY_NOON = new Date(2026, 9, 28, 12, 0);

test("no schedule means no next change", () => {
  assert.equal(nextChange(null, 20, WEDNESDAY_NOON), null);
});

test("finds the next entry later today", () => {
  const next = nextChange(week([360, 21], [1020, 18]), 21, WEDNESDAY_NOON);
  assert.deepEqual(next, { at: new Date(2026, 9, 28, 17, 0), temp: 18, days: 0 });
});

test("an entry at this very minute has already happened", () => {
  const next = nextChange(week([720, 21], [1020, 18]), undefined, WEDNESDAY_NOON);
  assert.equal(next.temp, 18);
});

test("rolls over to tomorrow", () => {
  const next = nextChange(week([360, 21]), 18, new Date(2026, 9, 28, 23, 0));
  assert.deepEqual(next, { at: new Date(2026, 9, 29, 6, 0), temp: 21, days: 1 });
});

test("rolls over the end of a month", () => {
  const next = nextChange(week([360, 21]), 18, new Date(2026, 9, 31, 23, 0));
  assert.deepEqual(next.at, new Date(2026, 10, 1, 6, 0));
});

test("Monday is the first list, whatever day it is", () => {
  const schedule = [[[360, 15]], [], [], [], [], [], []];
  const next = nextChange(schedule, 20, new Date(2026, 10, 1, 12, 0)); // a Sunday
  assert.deepEqual(next, { at: new Date(2026, 10, 2, 6, 0), temp: 15, days: 1 });
});

test("entries at the current temperature are skipped", () => {
  const next = nextChange(week([480, 21], [780, 21], [840, 18]), 21, WEDNESDAY_NOON);
  assert.equal(next.at.getHours(), 14);
});

test("a schedule that never changes temperature has no change, but has a next entry", () => {
  const schedule = week([420, 12.5]);
  assert.equal(nextChange(schedule, 12.5, WEDNESDAY_NOON), null);
  assert.deepEqual(nextChange(schedule, undefined, WEDNESDAY_NOON).at, new Date(2026, 9, 29, 7, 0));
});

test("off (null) counts as a temperature of its own", () => {
  const next = nextChange(week([1020, null], [1200, 18]), null, WEDNESDAY_NOON);
  assert.equal(next.temp, 18);
  assert.equal(nextChange(week([1020, null]), 18, WEDNESDAY_NOON).temp, null);
});

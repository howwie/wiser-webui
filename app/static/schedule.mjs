// The room's next schedule entry after now, as { at: Date, temp, days }. The schedule is 7 lists
// (Monday first) of [minutes after midnight, degrees or null for off], in the hub's local time,
// which is this browser's local time too. With `current` given, entries that keep the same
// temperature are skipped, so "until" means until the temperature actually changes.
export function nextChange(schedule, current, now = new Date()) {
  if (!schedule) return null;
  const today = (now.getDay() + 6) % 7;
  const nowMinutes = now.getHours() * 60 + now.getMinutes();
  for (let days = 0; days <= 7; days++) {
    for (const [minutes, temp] of schedule[(today + days) % 7]) {
      if ((days === 0 && minutes <= nowMinutes) || temp === current) continue;
      const at = new Date(now);
      at.setDate(now.getDate() + days);
      at.setHours(Math.floor(minutes / 60), minutes % 60, 0, 0);
      return { at, temp, days };
    }
  }
  return null;
}

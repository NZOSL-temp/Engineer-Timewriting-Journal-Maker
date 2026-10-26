// A few seconds of fireworks over the page, to celebrate a saved timesheet or a closed
// month. It never gets in the way (clicks go straight through), and it's skipped for
// anyone whose computer is set to reduce motion.

const COLOURS = ['#f43f5e', '#f59e0b', '#facc15', '#22c55e', '#14b8a6', '#3b82f6', '#a855f7', '#ec4899'];
let running = null;

export function fireworks({ bursts = 6 } = {}) {
  if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
  running?.stop();
  const canvas = document.createElement('canvas');
  canvas.className = 'fireworks';
  canvas.setAttribute('aria-hidden', 'true');
  document.body.append(canvas);
  const ctx = canvas.getContext('2d');
  if (!ctx) {
    canvas.remove();
    return;
  }
  let width = 0;
  let height = 0;
  const resize = () => {
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  };
  resize();
  window.addEventListener('resize', resize);

  const random = (low, high) => low + Math.random() * (high - low);
  const rockets = Array.from({ length: bursts }, (_, i) => ({
    launch: i * 280 + random(0, 160), // ms after the start
    x: width * random(0.15, 0.85),
    y: height + 8,
    vx: random(-0.04, 0.04), // px per ms
    vy: -random(0.75, 1.0) * Math.sqrt(height) / 22,
    top: height * random(0.18, 0.45),
    colour: COLOURS[Math.floor(Math.random() * COLOURS.length)],
    burst: false,
  }));
  const sparks = [];
  const start = performance.now();
  let last = start;
  let frame = 0;

  function explode(rocket) {
    rocket.burst = true;
    const count = Math.round(random(60, 90));
    const speed = random(0.22, 0.34);
    const other = COLOURS[Math.floor(Math.random() * COLOURS.length)];
    for (let i = 0; i < count; i += 1) {
      const angle = (i / count) * Math.PI * 2 + random(-0.08, 0.08);
      const v = speed * random(0.55, 1);
      sparks.push({
        x: rocket.x, y: rocket.y, px: rocket.x, py: rocket.y,
        vx: Math.cos(angle) * v, vy: Math.sin(angle) * v,
        life: random(900, 1500), age: 0, colour: Math.random() < 0.75 ? rocket.colour : other,
      });
    }
  }

  function step(now) {
    const dt = Math.min(now - last, 40);
    last = now;
    const t = now - start;
    ctx.clearRect(0, 0, width, height);
    for (const rocket of rockets) {
      if (rocket.burst || t < rocket.launch) continue;
      const px = rocket.x;
      const py = rocket.y;
      rocket.x += rocket.vx * dt;
      rocket.y += rocket.vy * dt;
      rocket.vy += 0.00045 * dt;
      ctx.strokeStyle = rocket.colour;
      ctx.lineWidth = 2.5;
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.moveTo(px, py + 6);
      ctx.lineTo(rocket.x, rocket.y);
      ctx.stroke();
      if (rocket.y <= rocket.top || rocket.vy >= 0) explode(rocket);
    }
    for (let i = sparks.length - 1; i >= 0; i -= 1) {
      const s = sparks[i];
      s.age += dt;
      if (s.age >= s.life) {
        sparks.splice(i, 1);
        continue;
      }
      s.px = s.x;
      s.py = s.y;
      s.vx *= 0.985 ** (dt / 16);
      s.vy = s.vy * 0.985 ** (dt / 16) + 0.00018 * dt;
      s.x += s.vx * dt;
      s.y += s.vy * dt;
      const fade = Math.max(0, 1 - s.age / s.life);
      ctx.globalAlpha = fade;
      ctx.strokeStyle = s.colour;
      ctx.fillStyle = s.colour;
      ctx.lineWidth = 3;
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.moveTo(s.px - (s.x - s.px) * 2, s.py - (s.y - s.py) * 2); // a short tail
      ctx.lineTo(s.x, s.y);
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(s.x, s.y, 1.2 + 1.6 * fade, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
    if (rockets.every((r) => r.burst) && !sparks.length) return stop();
    frame = requestAnimationFrame(step);
  }

  function stop() {
    cancelAnimationFrame(frame);
    window.removeEventListener('resize', resize);
    canvas.remove();
    if (running?.canvas === canvas) running = null;
  }

  running = { canvas, stop };
  frame = requestAnimationFrame(step);
}

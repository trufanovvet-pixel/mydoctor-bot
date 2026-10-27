'use strict';
(() => {
  const messages = JSON.parse(document.querySelector('#ui-messages')?.textContent || '{}');
  const t = text => messages[text] || text;
  const now = new Date();
  const pad = n => String(n).padStart(2, '0');
  const localDate = `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}`;
  document.querySelectorAll('[data-care-timezone]').forEach(input => { if (!input.value) input.value = Intl.DateTimeFormat().resolvedOptions().timeZone; });
  document.querySelectorAll('[data-care-date]').forEach(input => { if (!input.value) input.value = localDate; });
  document.querySelectorAll('[data-care-datetime]').forEach(input => { if (!input.value) input.value = `${localDate}T${pad(now.getHours())}:${pad(now.getMinutes())}`; });
  document.querySelectorAll('form[data-confirm]').forEach(form => form.addEventListener('submit', event => { if (!window.confirm(form.dataset.confirm)) event.preventDefault(); }));
  const picker = document.querySelector('[data-prescription-picker]');
  picker?.addEventListener('change', () => {
    if (picker.value === '') return;
    const medicines = JSON.parse(document.querySelector('#care-prescription').textContent);
    const chosen = medicines[Number(picker.value)];
    const form = document.querySelector('#care-medication-form');
    for (const key of ['name', 'dose', 'instructions', 'times']) form.elements[key].value = chosen[key] || '';
    form.elements.confirmed.checked = false;
  });
  const kind = document.querySelector('[data-care-kind]');
  function showKind() {
    document.querySelectorAll('[data-care-postop]').forEach(node => { node.hidden = kind.value !== 'postop'; });
    document.querySelectorAll('[data-care-chronic]').forEach(node => { node.hidden = kind.value !== 'chronic'; });
  }
  if (kind) { kind.addEventListener('change', showKind); showKind(); }
  const flags = [...document.querySelectorAll('input[name="flags"]')];
  function showUrgency() { const notice = document.querySelector('[data-care-emergency]'); if (notice) notice.hidden = !flags.some(input => input.checked); }
  flags.forEach(input => input.addEventListener('change', showUrgency)); showUrgency();
  const invitation = document.querySelector('[data-invite-link]');
  if (invitation) invitation.value = new URL(invitation.value, window.location.origin).href;
  document.querySelector('[data-copy-invite]')?.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(invitation.value); document.querySelector('[data-copy-status]').textContent = t('Ссылка скопирована.'); }
    catch (_) { invitation.focus(); invitation.select(); document.querySelector('[data-copy-status]').textContent = t('Скопируйте выделенную ссылку.'); }
  });
  // Expand the form that an operation shortcut or an in-page link opens.
  function expandTarget() { const el = document.getElementById(window.location.hash.slice(1)); if (el?.tagName === 'DETAILS') el.open = true; }
  window.addEventListener('hashchange', expandTarget); expandTarget();
  const carePanel = document.querySelector('[data-care-pet]');
  if (carePanel) {
    const doseRows = [...carePanel.querySelectorAll('[data-dose-id]')];
    async function refreshDoses() {
      if (document.hidden || !doseRows.length) return;
      try {
        const params = new URLSearchParams(); doseRows.forEach(row => params.append('id', row.dataset.doseId));
        const response = await fetch(`/care/pets/${carePanel.dataset.carePet}/dose-status?${params}`, {credentials:'same-origin', cache:'no-store'});
        if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) return;
        const data = await response.json();
        for (const dose of data.doses) if (dose.state !== 'pending') {
          const row = doseRows.find(el => el.dataset.doseId === String(dose.id));
          row.querySelector('[data-dose-status]').textContent = dose.label + (dose.actor ? ' · '+dose.actor : '');
          row.querySelectorAll('button').forEach(button => { button.disabled = true; });
        }
      } catch (_) { /* The server rechecks the current state on every submission. */ }
    }
    setInterval(refreshDoses, 15000); window.addEventListener('focus', refreshDoses); refreshDoses();
  }
  const chartNode = document.querySelector('#care-chart-data');
  if (!chartNode) return;
  const rows = JSON.parse(chartNode.textContent);
  const host = document.querySelector('[data-care-charts]');
  function svgNode(name, attrs, text) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', name);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (text !== undefined) node.textContent = text;
    return node;
  }
  for (const [key, label] of [['weight','Вес, кг'], ['vomiting','Рвота за день, эпизодов'], ['seizures','Приступы за день, эпизодов']]) {
    const points = rows.filter(row => row[key] !== null).map(row => ({at:new Date(row.at).getTime(), value:row[key]}));
    if (!points.length) continue;
    const card = document.createElement('div'); card.className = 'care-chart';
    const title = document.createElement('h3'); title.textContent = t(label); card.append(title);
    const svg = svgNode('svg', {viewBox:'0 0 300 155', role:'img', 'aria-label':t(label)});
    const min = Math.min(...points.map(p => p.value)); const max = Math.max(...points.map(p => p.value));
    const lo = min === max ? Math.max(0,min-1) : min; const hi = max === lo ? max+1 : max;
    const first = points[0].at; const last = points[points.length-1].at;
    const x = point => last === first ? 165 : 40+(point.at-first)/(last-first)*240;
    const y = point => 120-(point.value-lo)/(hi-lo)*100;
    svg.append(svgNode('line',{x1:40,x2:280,y1:120,y2:120}), svgNode('text',{x:2,y:24},String(hi)),svgNode('text',{x:2,y:122},String(lo)));
    // Individual observations, not daily totals; retain missing-data gaps.
    for (let i=1; i<rows.length; i++) if (rows[i-1][key] !== null && rows[i][key] !== null) {
      const a = {at:new Date(rows[i-1].at).getTime(),value:rows[i-1][key]};
      const b = {at:new Date(rows[i].at).getTime(),value:rows[i][key]};
      svg.append(svgNode('polyline',{points:`${x(a)},${y(a)} ${x(b)},${y(b)}`}));
    }
    points.forEach(point => { const circle=svgNode('circle',{cx:x(point),cy:y(point),r:3});circle.append(svgNode('title',{},`${new Date(point.at).toLocaleString()}: ${point.value}`));svg.append(circle); });
    svg.append(svgNode('text',{x:40,y:148},new Date(first).toLocaleDateString()),svgNode('text',{x:280,y:148,'text-anchor':'end'},new Date(last).toLocaleDateString()));
    card.append(svg); host.append(card);
  }
})();

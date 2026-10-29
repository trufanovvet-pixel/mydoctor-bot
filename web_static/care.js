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
  document.querySelectorAll('form[data-wound-analysis]').forEach(form => form.addEventListener('submit', () => {
    form.querySelector('button').disabled = true;
    form.querySelector('[data-wound-status]').textContent = t('Рассматриваю фото… Это может занять до двух минут.');
  }));
  document.querySelectorAll('[data-medication-times]').forEach((timesInput, index) => {
    let setMedicationTimes = null;
    const fallback = timesInput.closest('label');
    const group = document.createElement('fieldset');
    group.className = 'care-full care-time-picker';
    const legend = document.createElement('legend');
    legend.textContent = t('Во сколько давать лекарство?');
    const hint = document.createElement('p');
    hint.textContent = t('Выберите время для каждого приёма по назначению врача.');
    const rows = document.createElement('div');
    const add = document.createElement('button');
    add.type = 'button'; add.className = 'secondary';
    add.textContent = t('＋ Добавить время');
    const error = document.createElement('p');
    error.id = 'medication-time-error-' + index; error.setAttribute('role', 'alert'); error.hidden = true;
    group.append(legend, hint, rows, add, error);
    fallback.after(group);
    function sync() {
      const inputs = [...rows.querySelectorAll('input')];
      const values = inputs.map(input => input.value);
      const duplicate = values.some((v, i) => v && values.indexOf(v) !== i);
      const message = duplicate ? t('Это время уже добавлено. Выберите другое.') : '';
      inputs.forEach(input => input.setCustomValidity(message));
      error.textContent = message; error.hidden = !duplicate;
      timesInput.value = values.join(', ');
      add.disabled = inputs.length >= 6;
      rows.querySelectorAll('button').forEach(button => { button.hidden = inputs.length === 1; });
    }
    function addRow(value = '') {
      const row = document.createElement('div'); row.className = 'care-time-row';
      const label = document.createElement('label');
      const input = document.createElement('input');
      label.append(document.createTextNode(t('Время приёма')));
      input.type = 'time'; input.required = true; input.step = '60'; input.value = value.trim();
      input.setAttribute('aria-describedby', error.id);
      input.addEventListener('input', sync); input.addEventListener('change', sync);
      label.append(input);
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'secondary'; remove.textContent = t('Удалить время');
      remove.addEventListener('click', () => { row.remove(); sync(); });
      row.append(label, remove); rows.append(row);
      sync(); return input;
    }
    setMedicationTimes = value => {
      rows.replaceChildren();
      const values = (value || '').split(',').map(v => v.trim()).filter(Boolean);
      (values.length ? values : ['']).forEach(addRow);
    };
    const original = timesInput.value;
    timesInput.required = false; timesInput.type = 'hidden'; fallback.hidden = true;
    setMedicationTimes(original);
    add.addEventListener('click', () => { if (rows.children.length < 6) addRow().focus(); });
    timesInput.form.addEventListener('submit', sync);
  });
  const medicationError = document.querySelector('[data-medication-error]');
  if (medicationError) {
    document.querySelector('#new-medication').open = true;
    medicationError.focus();
    medicationError.scrollIntoView({block: 'center'});
  }
  document.querySelectorAll('[data-medication-card]').forEach(card => {
    const start = card.querySelector('[data-course-start]');
    const duration = card.querySelector('[data-course-duration]');
    const end = card.querySelector('[data-course-end]');
    const hint = card.querySelector('[data-course-end-hint]');
    if (!duration.value) {
      const instructions = card.querySelector('[data-course-instructions]').value;
      const match = instructions.match(/(?:^|\s)(\d{1,3})\s*(дней|дня|день|суток|сутки|days?|недели|недель|неделю|weeks?)(?=\s|[.,;]|$)/i);
      if (match) duration.value = Number(match[1]) * (/нед|week/i.test(match[2]) ? 7 : 1);
    }
    function calculateEnd() {
      const days = Number(duration.value);
      if (!start.value || !Number.isInteger(days) || days < 1 || days > 180) {
        end.readOnly = false; hint.textContent = ''; return;
      }
      const date = new Date(start.value + 'T12:00:00Z');
      date.setUTCDate(date.getUTCDate() + days - 1);
      end.value = date.toISOString().slice(0,10);
      end.readOnly = true;
      hint.textContent = t('Рассчитано автоматически, включая первый день курса.');
    }
    start.addEventListener('input', calculateEnd);
    duration.addEventListener('input', calculateEnd);
    calculateEnd();
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

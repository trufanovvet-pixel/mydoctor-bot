/* First-party navigation only. Business outcomes are recorded by the server. */
(() => {
  const app = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  document.cookie = `mydoctor_display=${app ? 'app' : 'web'}; Path=/; SameSite=Lax; Secure`;
  if (location.pathname.startsWith('/doctor')) return;
  document.addEventListener('submit', event => {
    const form=event.target;
    if (!(form instanceof HTMLFormElement) || new URL(form.action || location.href).origin !== location.origin) return;
    let field=form.querySelector('input[name="analytics_source"]');
    if (!field) {field=document.createElement('input');field.type='hidden';field.name='analytics_source';form.append(field);}
    field.value=app?'app':'web';
  },true);
  let page=location.pathname;
  for (const base of ['/pets','/analyses','/consultation','/billing']) if (page.startsWith(base+'/')) page=base;
  const allowed = ['/history','/messages','/operations','/profile','/how-it-works','/install','/', '/dashboard', '/app', '/register', '/login', '/pets', '/analyses', '/prevention', '/assistant', '/consultation', '/billing'];
  const csrf = document.querySelector('meta[name="analytics-csrf"]')?.content;
  if (!csrf || !allowed.includes(page)) return;
  function visit() {
    if (document.visibilityState !== 'visible') return;
    fetch('/api/analytics/events', {method:'POST', credentials:'same-origin', keepalive:true,
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({csrf, page, source:app?'app':'web', event_id:crypto.randomUUID()})}).catch(() => {});
  }
  visit();
  // Long-lived installed app resumes count as a new session after the server's idle timeout.
  document.addEventListener('visibilitychange', visit);
})();

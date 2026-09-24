(() => {
  'use strict';
  const t = text => window.uiText ? window.uiText(text) : text;
  const standalone = () => matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  // iOS may remember the page used for Add to Home Screen, not manifest.start_url.
  if (standalone() && location.pathname === '/doctor/install') {
    location.replace('/doctor/dashboard'); return;
  }
  let installPrompt = null;
  const installButton = document.querySelector('[data-install-button]');
  const installStatus = document.querySelector('[data-install-status]');
  const installed = document.querySelector('[data-installed]');
  function renderInstall() {
    if (installButton) installButton.hidden = standalone() || !installPrompt;
    if (installed) installed.hidden = !standalone();
    const instructions = document.querySelector('[data-install-instructions]');
    if (instructions) instructions.hidden = standalone();
    document.querySelectorAll('[data-install-callout]').forEach(node => node.hidden = standalone());
    if (standalone()) document.querySelectorAll('.app-nav').forEach(link => link.textContent = t('Приложение и уведомления'));
  }
  addEventListener('beforeinstallprompt', event => {
    if (!installButton) return; // Other pages keep the browser's own installation UI.
    event.preventDefault(); installPrompt = event; renderInstall();
  });
  addEventListener('appinstalled', () => {
    installPrompt = null; renderInstall();
    if (installStatus) installStatus.textContent = t('Готово! Откройте МойДоктор с иконки на экране телефона.');
  });
  if (installButton) installButton.addEventListener('click', async () => {
    if (!installPrompt) return;
    installButton.disabled = true;
    try {
      await installPrompt.prompt();
      const choice = await installPrompt.userChoice;
      if (installStatus) installStatus.textContent = t(choice.outcome === 'accepted' ? 'Готово! Откройте МойДоктор с иконки на экране телефона.' : 'Установку можно повторить через меню браузера.');
    } catch (_) {
      if (installStatus) installStatus.textContent = t('Установку можно повторить через меню браузера.');
    } finally { installPrompt = null; installButton.disabled = false; renderInstall(); }
  });
  renderInstall();
  const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const platform = ios ? 'ios' : /Android/.test(navigator.userAgent) ? 'android' : 'desktop';
  const guide = document.querySelector('[data-platform="'+platform+'"]');
  if (guide) guide.open = true;
  const setup = document.querySelector('[data-app-setup]');
  const pushButton = document.querySelector('[data-push-button]');
  const pushStatus = document.querySelector('[data-push-status]');
  function status(text) { if (pushStatus) pushStatus.textContent = t(text); }
  if (!('serviceWorker' in navigator) || !isSecureContext) {
    status('Этот браузер не поддерживает установку и уведомления. Откройте сайт в обновлённом браузере.'); return;
  }
  const ready = navigator.serviceWorker.register('/service-worker.js', {scope: '/', updateViaCache: 'none'})
    .then(registration => {
      registration.update().catch(() => {});
      return Promise.race([navigator.serviceWorker.ready, new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 15000))]);
    });
  // Registration errors must not affect the rest of the site.
  ready.catch(() => status('Не удалось подготовить приложение. Проверьте интернет и обновите страницу.'));
  if (!setup || !pushButton) {
    const role = document.querySelector('meta[name="mydoctor-push-role"]')?.content;
    if (role && 'PushManager' in window && 'Notification' in window && Notification.permission === 'granted') {
      ready.then(async registration => {
        const subscription = await registration.pushManager.getSubscription();
        if (!subscription) return;
        const config = await api('/api/push/config?role='+role);
        // Refresh only this account's existing opt-in. Never silently enable it for a new account.
        if (config.enabled) await api('/api/push/subscription', {method: 'POST',
          headers: {'Content-Type': 'application/json', 'X-PWA-CSRF': config.csrf},
          body: JSON.stringify({role, action: 'enable', subscription: subscription.toJSON()})});
      }).catch(() => {});
    }
    return;
  }
  if (ios && !standalone()) {
    status('Сначала добавьте МойДоктор на главный экран и откройте его с иконки.'); return;
  }
  if (!('PushManager' in window) || !('Notification' in window)) {
    status('Уведомления в этом браузере недоступны. Переписка по-прежнему доступна на сайте.'); return;
  }
  let registration, config, enabled = false;
  async function api(path, options = {}) {
    const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(path, {...options, signal: controller.signal, credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) throw new Error(response.status === 401 || response.status === 403 ? 'auth' : 'network');
      return await response.json();
    } finally { clearTimeout(timer); }
  }
  const save = (action, subscription) => api('/api/push/subscription', {method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-PWA-CSRF': config.csrf},
    body: JSON.stringify({role: setup.dataset.role, action, subscription})});
  const bytes = value => {
    const raw = atob(value.replace(/-/g, '+').replace(/_/g, '/')+'='.repeat((4-value.length%4)%4));
    return Uint8Array.from(raw, char => char.charCodeAt(0));
  };
  function renderPush() {
    pushButton.textContent = t(enabled ? 'Отключить уведомления' : 'Включить уведомления');
    pushButton.disabled = !enabled && Notification.permission === 'denied';
    status(enabled ? 'Уведомления включены на этом устройстве.' : Notification.permission === 'denied'
      ? 'Уведомления заблокированы. Разрешите их в настройках браузера или приложения и обновите страницу.'
      : 'Включите уведомления, чтобы узнавать о новых сообщениях.');
  }
  ready.then(async value => {
    registration = value;
    config = await api('/api/push/config?role='+setup.dataset.role);
    const subscription = await registration.pushManager.getSubscription();
    enabled = !!(config.enabled && subscription && Notification.permission === 'granted');
    if (enabled) await save('enable', subscription.toJSON());
    else if (config.enabled && !subscription) await save('disable');
    renderPush();
  }).catch(error => status(error.message === 'auth' ? 'Войдите в аккаунт и обновите страницу.' : 'Не удалось проверить уведомления. Проверьте интернет и обновите страницу.'));
  pushButton.addEventListener('click', async () => {
    if (!registration || !config) return;
    pushButton.disabled = true;
    try {
      if (enabled) {
        // Disable the server binding first, even if browser unsubscribe fails.
        await save('disable'); enabled = false;
        const subscription = await registration.pushManager.getSubscription();
        if (subscription) await subscription.unsubscribe();
        renderPush(); return;
      }
      // Permission is requested directly from this click, required on iOS.
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') { renderPush(); return; }
      let subscription = await registration.pushManager.getSubscription();
      if (!subscription) subscription = await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: bytes(config.public_key)});
      await save('enable', subscription.toJSON()); enabled = true; renderPush();
    } catch (error) {
      pushButton.disabled = false;
      status(error.message === 'auth' ? 'Войдите в аккаунт и обновите страницу.' : 'Не удалось изменить уведомления. Проверьте интернет и попробуйте ещё раз.');
    }
  });
})();

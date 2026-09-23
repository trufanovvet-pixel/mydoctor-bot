const requestForm = document.querySelector('[data-consultation-form]');
if (requestForm) {
  const pet = requestForm.querySelector('[data-booking-pet]');
  if (pet) {
    function syncPet() {
      const manual = requestForm.querySelector('[data-manual-pet]');
      manual.hidden = Boolean(pet.value);
      manual.querySelector('input').required = !pet.value;
      requestForm.querySelectorAll('[data-document-pet]').forEach(row => {
        const incompatible = Boolean(pet.value && row.dataset.documentPet && row.dataset.documentPet !== pet.value);
        row.hidden = incompatible;
        row.querySelector('input').disabled = incompatible;
        if (incompatible) row.querySelector('input').checked = false;
      });
    }
    pet.addEventListener('change', syncPet);
    syncPet();
  }
  requestForm.addEventListener('submit', (event) => {
    if (requestForm.dataset.submitting) { event.preventDefault(); return; }
    requestForm.dataset.submitting = 'true';
    requestForm.querySelector('button[type=submit]').disabled = true;
    requestForm.querySelector('[data-submit-status]').textContent = window.uiText('Сохраняем заявку…');
  });
  window.addEventListener('pageshow', () => {
    delete requestForm.dataset.submitting;
    requestForm.querySelector('button[type=submit]').disabled = false;
    requestForm.querySelector('[data-submit-status]').textContent = '';
  });
}
const confirmation = document.querySelector('[data-request-status]');
if (confirmation && ['pending', 'sending', 'retry'].includes(confirmation.dataset.requestStatus)) {
  let attempts = 0;
  async function refreshStatus() {
    attempts += 1;
    try {
      const response = await fetch(confirmation.dataset.statusUrl, {cache: 'no-store'});
      if (!response.ok || response.redirected) throw new Error('Status unavailable');
      const status = await response.json();
      confirmation.dataset.requestStatus = status.state;
      confirmation.querySelector('[data-delivery-message]').textContent = status.message;
      const workflow = confirmation.querySelector('[data-workflow-status]');
      if (workflow && status.workflow_status) workflow.textContent = status.workflow_status;
      const hint = confirmation.querySelector('[data-delivery-hint]');
      if (status.state === 'delivered') {
        if (hint) hint.textContent = window.uiText('Уведомление доставлено врачу в Telegram.');
        return;
      }
      if (hint && status.state === 'retry') hint.textContent = window.uiText('Доставка задерживается. Повторим автоматически; новую заявку создавать не нужно.');
    } catch (_) { /* The saved request remains available through the refresh link. */ }
    if (attempts < 24) setTimeout(refreshStatus, 5000);
    else {
      const hint = confirmation.querySelector('[data-delivery-hint]');
      if (hint) hint.textContent = window.uiText('Заявка сохранена. Проверить доставку можно по ссылке «Обновить статус».');
    }
  }
  setTimeout(refreshStatus, 1500);
}

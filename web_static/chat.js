(() => {
  'use strict';
  const form = document.getElementById('chat'), message = document.getElementById('message');
  const box = document.getElementById('messages'), clear = document.getElementById('clear-chat');
  let busy = false, pendingKey = null, pendingText = null;
  function bubble(text, kind) {
    const el = document.createElement('div'); el.className = 'msg ' + kind; el.textContent = text;
    box.appendChild(el); return el;
  }
  function requestKey() {
    const bytes = new Uint8Array(16); crypto.getRandomValues(bytes);
    return Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
  }
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (busy) return;
    const text = message.value.trim(); if (!text) return;
    if (!pendingKey || pendingText !== text) { pendingKey = requestKey(); pendingText = text; }
    busy = true; form.querySelector('button').disabled = true; clear.disabled = true;
    box.querySelector('.chat-empty')?.remove(); bubble(text, 'user'); message.value = '';
    const answer = bubble(window.uiText('Формирую ответ…'), 'bot');
    try {
      const response = await fetch('/api/chat', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message: text, request_key: pendingKey})});
      const data = await response.json();
      answer.textContent = response.status === 401 ? window.uiText('Сессия завершена. Войдите в кабинет заново.') :
        data.answer || data.error || window.uiText('Не удалось получить ответ.');
      if (response.ok) { pendingKey = null; pendingText = null; }
      if (response.status === 402) {
        const link = document.createElement('a'); link.href = '/billing'; link.textContent = window.uiText('Тариф и оплаты');
        link.style.display = 'block'; answer.appendChild(link);
      }
      if (!response.ok && !message.value) message.value = text;
    } catch (error) {
      answer.textContent = window.uiText('Не удалось получить ответ. Проверьте соединение и попробуйте ещё раз.');
      if (!message.value) message.value = text;
    } finally {
      busy = false; form.querySelector('button').disabled = false; clear.disabled = false; box.scrollTop = box.scrollHeight;
    }
  });
  clear.addEventListener('click', async () => {
    if (busy) return; clear.disabled = true;
    try {
      const response = await fetch('/api/chat/clear', {method: 'POST'}); if (!response.ok) throw new Error();
      pendingKey = null; pendingText = null; box.replaceChildren();
      bubble(window.uiText('Начинаем новый разговор. Что вас беспокоит?'), 'bot');
    } catch (error) {
      bubble(window.uiText('Не удалось начать новый разговор. Попробуйте ещё раз.'), 'bot');
    } finally { clear.disabled = false; }
  });
  box.scrollTop = box.scrollHeight;
})();

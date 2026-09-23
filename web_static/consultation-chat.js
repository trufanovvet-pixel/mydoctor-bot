(() => {
  const tr = value => window.uiText(value);
  async function requestWithTimeout(url,options={}) {
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),30000);
    try{return await fetch(url,{...options,signal:controller.signal});}finally{clearTimeout(timer);}
  }
  const badgeRoot = document.querySelector('[data-message-notifications]');
  async function refreshBadges() {
    if (!badgeRoot || document.hidden) return;
    try {
      const res = await requestWithTimeout('/messages/unread?role=' + badgeRoot.dataset.messageNotifications, {cache:'no-store'});
      if (!res.ok || res.redirected) return;
      const data = await res.json();
      document.querySelectorAll('[data-unread-total]').forEach(el => {el.textContent=data.count; el.hidden=!data.count;});
      document.querySelectorAll('[data-request-unread]').forEach(el => {
        const count=data.requests[el.dataset.requestUnread] || 0; el.textContent=count; el.hidden=!count;
      });
    } catch (_) {}
  }
  if (badgeRoot) {refreshBadges(); setInterval(refreshBadges, 15000);}
  const chat = document.querySelector('[data-conversation]');
  if (!chat) return;
  const list=chat.querySelector('[data-chat-list]'), form=chat.querySelector('[data-chat-form]');
  const feedback=chat.querySelector('[data-chat-feedback]'), older=chat.querySelector('[data-chat-older]');
  const connection=chat.querySelector('[data-chat-connection]');
  const newButton=chat.querySelector('[data-chat-new]'), role=chat.dataset.role;
  let cursor=Math.max(0,...Array.from(list.querySelectorAll('[data-message-id]'),el=>Number(el.dataset.messageId)));
  let otherSeen=Number(chat.dataset.otherSeen), readThrough=0, pollBusy=false, sending=false, visible=false;
  const atBottom=()=>list.scrollHeight-list.scrollTop-list.clientHeight<80;
  function localTimes() {
    list.querySelectorAll('[data-chat-time]').forEach(el=>{
      const date=new Date(el.dateTime);
      if (!isNaN(date)) el.textContent=new Intl.DateTimeFormat(document.documentElement.lang,{dateStyle:'short',timeStyle:'short'}).format(date);
    });
  }
  function receipts() {
    list.querySelectorAll('[data-message-receipt]').forEach(el=>{
      const id=Number(el.closest('[data-message-id]').dataset.messageId);
      el.textContent=tr(id<=otherSeen?'Прочитано':'Отправлено');
    });
  }
  async function markRead() {
    if (!visible || document.hidden || !atBottom() || cursor<=readThrough || !cursor) return;
    const seen=cursor;
    try {
      const res=await requestWithTimeout(chat.dataset.readUrl,{method:'POST',headers:{'X-Message-CSRF':chat.dataset.csrf},body:new URLSearchParams({last_id:seen})});
      if (res.ok) {readThrough=Math.max(readThrough,seen); refreshBadges();}
    } catch (_) {}
  }
  function append(message) {
    if (list.querySelector('[data-message-id="'+message.id+'"]')) return;
    list.querySelector('[data-chat-empty]')?.remove();
    const article=document.createElement('article');article.className='patient-message '+(message.role===role?'mine':'theirs');
    article.dataset.messageId=message.id;article.dataset.messageRole=message.role;
    const name=document.createElement('strong');name.textContent=tr(message.role===role?'Вы':message.role==='doctor'?'Врач':'Владелец');article.append(name);
    if (message.body) {const p=document.createElement('p');p.textContent=message.body;article.append(p);}
    for (const file of message.files) {const a=document.createElement('a');a.className='chat-file';a.href=file.url;a.textContent=file.filename+' ↓';article.append(a);}
    const footer=document.createElement('footer'),time=document.createElement('time');time.dataset.chatTime='';time.dateTime=message.created_at;footer.append(time);
    if (message.role===role) {const receipt=document.createElement('span');receipt.dataset.messageReceipt='';footer.append(receipt);}
    article.append(footer);
    const next=Array.from(list.children).find(el=>Number(el.dataset.messageId)>message.id);
    list.insertBefore(article,next || null);
  }
  async function poll() {
    if (pollBusy || document.hidden) return;
    pollBusy=true;
    try {
      for (let page=0;page<3;page++) {
        const res=await requestWithTimeout(chat.dataset.url+'?after='+cursor,{cache:'no-store'});
        if (!res.ok || res.redirected) throw new Error('unavailable');
        const data=await res.json(), bottom=atBottom();
        const workflow=document.querySelector('[data-workflow-status]'),paid=document.querySelector('[data-chat-paid]');
        if(workflow)workflow.textContent=data.workflow_status;
        if(paid)paid.hidden=!data.paid_confirmed;
        for (const message of data.messages) {append(message);cursor=Math.max(cursor,message.id);}
        otherSeen=data.other_seen;localTimes();receipts();
        if (bottom) list.scrollTop=list.scrollHeight;
        else if (data.messages.length) newButton.hidden=false;
        connection.textContent='';
        if (!data.more) break;
      }
      markRead();
    } catch (_) {connection.textContent=tr('Связь прервалась. Сообщения обновятся после подключения.');}
    finally {pollBusy=false;}
  }
  function newKey() {
    if (crypto.randomUUID) return crypto.randomUUID();
    return '10000000-1000-4000-8000-100000000000'.replace(/[018]/g,c=>(c^crypto.getRandomValues(new Uint8Array(1))[0]&15>>c/4).toString(16));
  }
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(sending)return;
    const payload=new FormData(form);
    sending=true;const button=form.querySelector('[type=submit]');button.disabled=true;
    form.elements.body.readOnly=true;form.elements.files.disabled=true;
    feedback.textContent=tr('Отправляем сообщение…');
    try {
      const res=await requestWithTimeout(form.action,{method:'POST',body:payload,headers:{'X-Chat-Request':'1'}});
      const data=await res.json().catch(()=>({}));
      if(!res.ok || res.redirected || !data.message)throw Object.assign(new Error(),{displayMessage:data.error});
      append(data.message);localTimes();receipts();list.scrollTop=list.scrollHeight;
      form.elements.body.value='';form.elements.files.value='';form.elements.request_key.value=newKey();
      feedback.textContent=tr('Сообщение отправлено.');
    } catch(error) {feedback.textContent=error.displayMessage || tr('Не удалось отправить. Текст сохранён в поле — попробуйте ещё раз.');}
    finally {sending=false;button.disabled=false;form.elements.body.readOnly=false;form.elements.files.disabled=false;}
    poll();
  });
  older.addEventListener('click',async()=>{
    older.disabled=true;
    try {
      const first=list.querySelector('[data-message-id]');
      const res=await requestWithTimeout(chat.dataset.url+'?before='+first.dataset.messageId,{cache:'no-store'});
      if(!res.ok || res.redirected)throw new Error();
      const data=await res.json(),height=list.scrollHeight,top=list.scrollTop;
      data.messages.forEach(append);localTimes();receipts();list.scrollTop=top+list.scrollHeight-height;older.hidden=!data.more;
    } catch(_) {feedback.textContent=tr('Не удалось загрузить историю. Попробуйте ещё раз.');}
    finally {older.disabled=false;}
  });
  newButton.addEventListener('click',()=>{list.scrollTop=list.scrollHeight;newButton.hidden=true;markRead();});
  list.addEventListener('scroll',()=>{if(atBottom()){newButton.hidden=true;markRead();}});
  new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;if(visible)markRead();},{threshold:0.2}).observe(list);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden){poll();refreshBadges();}});
  window.addEventListener('online',poll);
  localTimes();receipts();list.scrollTop=list.scrollHeight;poll();setInterval(poll,5000);
})();

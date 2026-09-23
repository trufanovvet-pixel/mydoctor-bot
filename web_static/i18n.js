(() => {
  const node = document.getElementById('ui-messages');
  const messages = node ? JSON.parse(node.textContent) : {};
  window.uiText = text => messages[text] || text;
})();

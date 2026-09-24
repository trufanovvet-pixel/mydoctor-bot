document.querySelectorAll('[data-card-setup]').forEach(form => {
  const currency = form.querySelector('[name="currency"]');
  const prices = form.querySelector('[data-transfer-prices]');
  if (!prices) return;
  const update = (changed = false) => {
    const rubles = currency.value === 'RUB';
    prices.hidden = rubles;
    prices.querySelectorAll('input').forEach(input => {
      input.required = !rubles;
      input.disabled = rubles;
      if (changed) input.value = '';
    });
  };
  currency.addEventListener('change', () => update(true));
  update();
});

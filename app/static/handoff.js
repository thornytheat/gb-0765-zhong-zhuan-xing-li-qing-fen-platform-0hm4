const feedback = document.querySelector('#feedback');

async function call(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.message || '请求失败');
  return data;
}

function flash(message, ok = true) {
  feedback.textContent = message;
  feedback.className = ok ? '' : 'bad';
}

document.querySelector('#disposition-seed')?.addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.target.querySelector('button');
  button.disabled = true;
  try {
    const data = await call(event.target.action, {});
    flash(`已建 ${data.orders.length} 张处置单`);
    location.reload();
  } catch (error) {
    flash(error.message, false);
    button.disabled = false;
  }
});

document.querySelectorAll('.handoff-form').forEach(form => {
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button');
    const orderId = form.dataset.order;
    const body = Object.fromEntries(new FormData(form).entries());
    button.disabled = true;
    try {
      const data = await call(`/api/dispositions/${orderId}/handovers`, body);
      flash(data.replayed ? '同请求重放，未重复交接' : `已交接给 ${data.order.owner}`);
      location.reload();
    } catch (error) {
      flash(error.message, false);
      button.disabled = false;
    }
  });
});

document.querySelectorAll('.close-form').forEach(form => {
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      const data = await call(`/api/dispositions/${form.dataset.order}/close`, {});
      flash(data.replayed ? '关单请求重放' : '回放确认撤下，处置单已关单');
      location.reload();
    } catch (error) {
      flash(error.message, false);
      button.disabled = false;
    }
  });
});

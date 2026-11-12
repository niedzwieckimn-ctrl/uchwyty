/* Large architect archives go from the administrator's browser straight to private Storage. */
(function () {
  'use strict';
  const form = document.getElementById('program-upload');
  if (!form) return;
  const progress = document.getElementById('program-upload-progress');
  const status = document.getElementById('program-upload-status');
  const button = form.querySelector('button[type=submit]');
  const chunkSize = 6 * 1024 * 1024;
  const delays = [0, 3000, 5000, 10000, 20000];
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const metadata = value => btoa(value);

  function show(offset, total) {
    progress.value = Math.round(100 * offset / total);
    status.textContent = 'Przesyłanie: ' + progress.value + '%';
  }

  async function checked(response) {
    if (!response.ok) throw new Error('Serwer odrzucił przesyłanie (HTTP ' + response.status + ').');
    return response;
  }

  async function upload(file, grant) {
    const endpoint = new URL(grant.endpoint);
    if (endpoint.protocol !== 'https:' || !endpoint.hostname.endsWith('.supabase.co')) {
      throw new Error('Nieprawidłowy adres magazynu plików.');
    }
    const headers = { 'Tus-Resumable': '1.0.0', 'x-signature': grant.token };
    const created = await checked(await fetch(endpoint, {
      method: 'POST',
      headers: {
        ...headers,
        'Upload-Length': String(file.size),
        'Upload-Metadata': 'bucketName ' + metadata('architect-models-3d') +
          ',objectName ' + metadata(grant.key) +
          ',contentType ' + metadata('application/zip') +
          ',cacheControl ' + metadata('3600'),
        'x-upsert': 'true'
      }
    }));
    const location = created.headers.get('Location');
    if (!location) throw new Error('Magazyn nie zwrócił adresu przesyłania.');
    const uploadUrl = new URL(location, endpoint);
    if (uploadUrl.origin !== endpoint.origin) throw new Error('Nieprawidłowy adres przesyłania.');

    let offset = 0;
    while (offset < file.size) {
      let transferred = false;
      for (const delay of delays) {
        await pause(delay);
        try {
          const end = Math.min(file.size, offset + chunkSize);
          const response = await checked(await fetch(uploadUrl, {
            method: 'PATCH',
            headers: { ...headers, 'Content-Type': 'application/offset+octet-stream',
              'Upload-Offset': String(offset) },
            body: file.slice(offset, end)
          }));
          const next = Number(response.headers.get('Upload-Offset'));
          if (!Number.isInteger(next) || next <= offset || next > file.size) {
            throw new Error('Magazyn zwrócił nieprawidłowy postęp przesyłania.');
          }
          offset = next;
          show(offset, file.size);
          transferred = true;
          break;
        } catch (error) {
          // A lost PATCH response may still mean that the chunk was stored.
          try {
            const before = offset;
            const head = await checked(await fetch(uploadUrl, { method: 'HEAD', headers }));
            const remoteOffset = Number(head.headers.get('Upload-Offset'));
            if (Number.isInteger(remoteOffset) && remoteOffset >= offset && remoteOffset <= file.size) {
              offset = remoteOffset;
              show(offset, file.size);
              if (offset > before) {
                transferred = true;
                break;
              }
            }
          } catch { /* Try again while the signed upload is valid. */ }
        }
      }
      if (!transferred) throw new Error('Przesyłanie zostało przerwane. Sprawdź połączenie i spróbuj ponownie.');
    }
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (button.disabled) return;
    const file = form.elements.file.files[0];
    if (!file || !/\.zip$/i.test(file.name) || !file.size || file.size > 2 * 1024 * 1024 * 1024) {
      status.textContent = 'Wybierz archiwum ZIP nie większe niż 2 GB.';
      return;
    }
    const expected = form.elements.program.selectedOptions[0].dataset.filename;
    if (file.name !== expected) {
      status.textContent = 'Dla tego formatu wybierz plik ' + expected + '.';
      return;
    }
    button.disabled = true;
    progress.hidden = false;
    progress.value = 0;
    status.textContent = 'Przygotowuję przesyłanie…';
    try {
      const csrf = form.querySelector('input[name=csrf_token]')?.value;
      const response = await fetch(form.dataset.tokenUrl, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf || '' },
        body: JSON.stringify({ program: form.elements.program.value, filename: file.name, size: file.size })
      });
      const grant = await response.json();
      if (!response.ok || !grant.ok) throw new Error(grant.error || 'Nie udało się rozpocząć przesyłania.');
      await upload(file, grant);
      status.textContent = 'Paczka została przesłana. Odświeżam listę…';
      window.location.assign(new URL('?result=upload', window.location.href));
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : 'Nie udało się przesłać paczki.';
      button.disabled = false;
    }
  });
})();

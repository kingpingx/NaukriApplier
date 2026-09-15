/* Write the RESUME_JSON secret and start a scan, straight from the browser.
 *
 * GitHub only accepts a secret encrypted with the repository's public key
 * (a libsodium sealed box), so the value is sealed here and GitHub never sees
 * it in the clear. Needs a fine-grained token scoped to this one repository
 * with "Secrets: read and write" and "Actions: read and write". The token is
 * kept only if the user ticks "remember", and then only in this browser.
 */
'use strict';

(() => {
  // libsodium is what GitHub's docs use for secrets. jsDelivr's +esm build
  // bundles it as one ES module; the old single-file browser build is gone.
  const SODIUM = 'https://cdn.jsdelivr.net/npm/libsodium-wrappers@0.7.15/+esm';
  const API = 'https://api.github.com';
  const WORKFLOW = 'pages.yml';

  let sodiumPromise = null;
  function loadSodium() {
    sodiumPromise ||= import(SODIUM)
      .then(async (mod) => { const sodium = mod.default || mod; await sodium.ready; return sodium; })
      .catch((err) => { sodiumPromise = null; throw new Error(`could not load the encryption library (${err.message})`); });
    return sodiumPromise;
  }

  async function call(path, token, init = {}) {
    const res = await fetch(API + path, {
      ...init,
      headers: {
        Accept: 'application/vnd.github+json',
        Authorization: `Bearer ${token}`,
        'X-GitHub-Api-Version': '2022-11-28',
        ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      },
    });
    if (res.ok) return res.status === 204 || res.status === 201 ? null : res.json();
    const detail = (await res.json().catch(() => ({}))).message || res.statusText;
    const hint = {
      401: 'The token is invalid or expired.',
      403: 'The token lacks a permission: it needs "Secrets: read and write" and "Actions: read and write" on this repository.',
      404: 'Repository not found - check owner/repo, and that the token was given access to it.',
      422: 'GitHub rejected the request.',
    }[res.status] || '';
    throw new Error(`GitHub ${res.status}: ${hint || detail}`);
  }

  async function saveSecret(repo, token, name, value) {
    const sodium = await loadSodium();
    const key = await call(`/repos/${repo}/actions/secrets/public-key`, token);
    const sealed = sodium.crypto_box_seal(
      sodium.from_string(value), sodium.from_base64(key.key, sodium.base64_variants.ORIGINAL));
    await call(`/repos/${repo}/actions/secrets/${encodeURIComponent(name)}`, token, {
      method: 'PUT',
      body: JSON.stringify({ encrypted_value: sodium.to_base64(sealed, sodium.base64_variants.ORIGINAL),
                             key_id: key.key_id }),
    });
  }

  function runScan(repo, token, ref = 'main') {
    return call(`/repos/${repo}/actions/workflows/${WORKFLOW}/dispatches`, token, {
      method: 'POST', body: JSON.stringify({ ref }),
    });
  }

  // owner.github.io/repo/ -> "owner/repo"
  function guessRepo() {
    const host = location.hostname;
    const name = location.pathname.split('/').filter(Boolean)[0];
    return host.endsWith('.github.io') && name ? `${host.split('.')[0]}/${name}` : '';
  }

  window.GitHubSecrets = { saveSecret, runScan, guessRepo, loadSodium };
})();

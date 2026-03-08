// =============================================
// Table Tennis Scoreboard - Main JavaScript
// =============================================

const scoreA    = document.getElementById('scoreA');
const scoreB    = document.getElementById('scoreB');
const setsA     = document.getElementById('setsA');
const setsB     = document.getElementById('setsB');
const arrowA    = document.getElementById('arrowA');
const arrowB    = document.getElementById('arrowB');
const banner    = document.getElementById('banner');
const bannerText= document.getElementById('bannerText');
const bannerHistory = document.getElementById('bannerHistory');
const btnQuick  = document.getElementById('btnQuick');
const btnAdmin  = document.getElementById('btnAdmin');
const dlgQuick  = document.getElementById('dlgQuick');
const qForm     = document.getElementById('quickForm');
const qCancel   = document.getElementById('qCancel');
const qA        = document.getElementById('qa');
const qB        = document.getElementById('qb');
const qS        = document.getElementById('qs');
const qServ     = document.getElementById('qserv');

// ---- State Polling ----
async function poll() {
    try {
        const r = await fetch('/state', {cache:'no-store'});
        const s = await r.json();

        // Match bar
        const mb = document.getElementById('matchbar');
        if (s.match_active) {
            mb.style.display = 'block';
            document.getElementById('matchscore').textContent =
                s.match_home_team + ' ' + s.match_home_score + ' — ' + s.match_away_score + ' ' + s.match_away_team;
        } else {
            mb.style.display = 'none';
        }

        // Hide ends display when sets_to_win is 0
        const setsDisplay = s.sets_to_win === 0 ? 'none' : 'inline';
        if (setsA.parentElement) setsA.parentElement.style.display = setsDisplay;
        if (setsB.parentElement) setsB.parentElement.style.display = setsDisplay;

        // Shrink score font only when both are double digits
        const bothDouble = s.a >= 10 && s.b >= 10;
        const bigSize = bothDouble ? 'clamp(60px,26vw,400px)' : 'clamp(60px,38vw,500px)';
        document.querySelectorAll('.big').forEach(el => el.style.fontSize = bigSize);

        // Swap sides: end swap + mid-end swap + initial end selection
        const endSwap = s.sets_to_win > 0 ? s.swapped : false;
        const sw = endSwap !== (s.mid_end_display_swapped || false) !== (s.start_swapped || false);
        scoreA.textContent = sw ? s.b : s.a;
        scoreB.textContent = sw ? s.a : s.b;
        setsA.textContent  = sw ? s.sets_b : s.sets_a;
        setsB.textContent  = sw ? s.sets_a : s.sets_b;
        document.getElementById('nameA').textContent = sw ? s.name_b : s.name_a;
        document.getElementById('nameB').textContent = sw ? s.name_a : s.name_b;

        // Serve dots
        const leftServes = (s.server === 'A' && !sw) || (s.server === 'B' && sw);
        arrowA.style.display = leftServes ? 'block' : 'none';
        arrowB.style.display = leftServes ? 'none' : 'block';

        // Banner
        if (s.banner && s.banner.length) {
            const parts = s.banner.split('\n');
            bannerText.textContent = parts[0];
            if (bannerHistory && parts.length > 1) {
                bannerHistory.textContent = parts.slice(1).join(' ');
            }
            banner.classList.add('show');
        } else {
            banner.classList.remove('show');
        }
    } catch(e) {}
    finally { setTimeout(poll, 120); }
}
poll();

// ---- Command ----
async function cmd(c) {
    try {
        const r = await fetch('/key?cmd=' + c, {cache:'no-store'});
        if (r.status === 200) {
            const d = await r.json();
            if (d.redirect) { window.location.href = d.redirect; return; }
        }
    } catch(e) {}
}

// ---- A/L Key Handling (single/double/hold) ----
const dblMs  = window.DOUBLE_CLICK_MS || 350;
const longMs = window.LONG_PRESS_MS || 1000;
const keyState = {
    a: {down:false, t0:0, singleT:null, waitingDbl:false},
    l: {down:false, t0:0, singleT:null, waitingDbl:false}
};

function now() { return performance.now(); }

function handleDown(k, e) {
    e.preventDefault();
    const s = keyState[k];
    if (s.down) return;
    s.down = true; s.t0 = now();
}

function handleUp(k, plusCmd, minusCmd, e) {
    e.preventDefault();
    const s = keyState[k];
    if (!s.down) return;
    s.down = false;
    const held = now() - s.t0;
    if (held >= Math.max(400, longMs)) {
        if (s.singleT) clearTimeout(s.singleT);
        s.waitingDbl = false;
        cmd('next');
        return;
    }
    if (!s.waitingDbl) {
        s.waitingDbl = true;
        s.singleT = setTimeout(() => {
            s.waitingDbl = false; s.singleT = null;
            cmd(plusCmd);
        }, Math.max(150, dblMs));
    } else {
        if (s.singleT) { clearTimeout(s.singleT); s.singleT = null; }
        s.waitingDbl = false;
        cmd(minusCmd);
    }
}

// ---- Keyboard Shortcuts ----
let hCount = 0, hTimer = null;
let matchExitCount = 0, matchExitTimer = null;

document.addEventListener('keydown', (e) => {
    const tag = document.activeElement.tagName.toLowerCase();
    if (tag === 'input' || tag === 'select' || tag === 'textarea') return;

    const mb = document.getElementById('matchbar');
    const matchActive = mb !== null && mb.style.display !== 'none';

    if (matchActive) {
        // During match mode - only scoring keys + 5-press exit
        if (e.key === '5') {
            matchExitCount++;
            clearTimeout(matchExitTimer);
            matchExitTimer = setTimeout(() => { matchExitCount = 0; }, 5000);
            if (matchExitCount >= 5) {
                matchExitCount = 0;
                window.location.href = '/match/abandon';
            }
            return;
        }
        const k = e.key.toLowerCase();
        if (k === 'a') { handleDown('a', e); return; }
        if (k === 'l') { handleDown('l', e); return; }
        if (k === 'g') { e.preventDefault(); cmd('next'); return; }
        if (k === 's') { e.preventDefault(); cmd('toggleserve'); return; }
        e.preventDefault();
        return;
    }

    // Free play / normal mode - all keys work
    if (e.key.toLowerCase() === 'h') {
        hCount++;
        clearTimeout(hTimer);
        hTimer = setTimeout(() => { hCount = 0; }, 4000);
        if (hCount >= 6) { hCount = 0; window.location.href = '/secret'; return; }
    }

    const k = e.key.toLowerCase();
    if (k === 'escape')  { e.preventDefault(); window.location.href = '/admin'; }
    else if (k === 'g')  { e.preventDefault(); cmd('next'); }
    else if (k === 'n')  { e.preventDefault(); if (confirm('Start a NEW match? This resets ends and scores.')) cmd('resetmatch'); }
    else if (k === 's')  { e.preventDefault(); cmd('toggleserve'); }
    else if (k === 'c')  { e.preventDefault(); window.location.href = '/match/setup'; }
    else if (k === 'e')  { e.preventDefault(); dlgQuick.showModal(); qA.focus(); }
    else if (k === 'r' && !e.ctrlKey && !e.shiftKey && !e.metaKey)  { e.preventDefault(); fetch('/toggle_speech', {method:'POST'}).then(r=>r.json()).then(d=>{ const icon = document.getElementById('speechIcon'); if (icon) icon.textContent = d.speech ? 'SND ON' : 'SND OFF'; }); }
    else if (k === 'a')  { handleDown('a', e); }
    else if (k === 'l')  { handleDown('l', e); }
});

document.addEventListener('keyup', (e) => {
    const k = e.key.toLowerCase();
    if      (k === 'a') { handleUp('a', 'aplus', 'aminus', e); }
    else if (k === 'l') { handleUp('l', 'bplus', 'bminus', e); }
});

// ---- Quick Edit ----
qCancel.addEventListener('click', () => dlgQuick.close());

qForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(qForm);
    try {
        await fetch('/quick', { method:'POST', body: fd });
        dlgQuick.close();
        location.reload();
    } catch(err) {
        alert('Failed to save.');
    }
});

// ARKAINBRAIN Slot Renderer — Canvas-based, self-contained
// Uses MathModelController for spin results & win evaluation
(function(){
"use strict";

var CFG = window.GAME_CONFIG;
var SYMBOLS = window.SYMBOL_IMAGES;
var COLS = CFG.reelsCount, ROWS = CFG.rowsCount;

var canvas = document.getElementById('slot-canvas');
var ctx = canvas.getContext('2d');
var W, H, CELL_W, CELL_H, GRID_X, GRID_Y, GRID_W, GRID_H;

function resize() {
    var dpr = devicePixelRatio || 1;
    var container = canvas.parentElement;
    W = container.clientWidth; H = container.clientHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    GRID_W = Math.min(W * 0.92, 600); GRID_H = Math.min(H * 0.80, 400);
    CELL_W = GRID_W / COLS; CELL_H = GRID_H / ROWS;
    GRID_X = (W - GRID_W) / 2; GRID_Y = (H - GRID_H) / 2;
}
resize(); window.addEventListener('resize', resize);

var symbolImgs = [];
function loadSymbols() {
    for (var i = 0; i < SYMBOLS.length; i++) {
        var img = new Image();
        img.src = SYMBOLS[i];
        symbolImgs.push(img);
    }
}
loadSymbols();

// Math Engine
var math = new MathModelController({
    reelsCount: COLS, rowsCount: ROWS,
    targetRTP: CFG.targetRTP || 96, volatility: CFG.volatility || 'medium',
});
math.init(CFG);

// Free Spins Engine
var freeSpinsCtrl = null;
if (CFG.freeSpinRules && CFG.freeSpinRules.triggerSymbol !== undefined) {
    freeSpinsCtrl = new ComponentsFreeSpinsController({
        triggerSymbolId: CFG.freeSpinRules.triggerSymbol,
        minTriggerCount: CFG.freeSpinRules.minCount || 3,
        spinsAwarded: CFG.freeSpinRules.spinsAwarded || {3:10,4:15,5:25},
        retriggerEnabled: CFG.freeSpinRules.retriggerEnabled || false,
        multiplierMode: CFG.freeSpinRules.multiplierMode || 'fixed',
        baseMultiplier: CFG.freeSpinRules.baseMultiplier || 1,
        escalationStep: CFG.freeSpinRules.escalationStep || 1,
        maxMultiplier: CFG.freeSpinRules.maxMultiplier || 10,
    });
}

// Game State
var balance = 500, bet = 1, lines = CFG.betConfig ? CFG.betConfig.defaultLines : 20;
var gState = 'idle';
var matrix = null, lastWin = null;
var spinReels = [];
var winFlashTimer = 0, winPositions = [];
var particles = [];
var totalWinAmt = 0;
var autoplay = false, autoTimer = null;

var SPIN_DUR = 0.6, REEL_STAG = 0.15, EXTRA_SPINS = 3;

function ReelAnim(reelIdx, finalSymbols) {
    this.reel = reelIdx;
    this.finalSymbols = finalSymbols;
    this.duration = SPIN_DUR + reelIdx * REEL_STAG;
    this.elapsed = 0;
    this.offset = 0;
    this.done = false;
    this.strip = [];
    var padCount = (EXTRA_SPINS + 1) * ROWS;
    for (var i = 0; i < padCount; i++) {
        this.strip.push(Math.floor(Math.random() * symbolImgs.length));
    }
    for (var j = 0; j < finalSymbols.length; j++) this.strip.push(finalSymbols[j]);
    this.totalDistance = (this.strip.length - ROWS) * CELL_H;
}
ReelAnim.prototype.update = function(dt) {
    if (this.done) return;
    this.elapsed += dt;
    var t = Math.min(this.elapsed / this.duration, 1);
    t = 1 - Math.pow(1 - t, 3);
    this.offset = t * this.totalDistance;
    if (this.elapsed >= this.duration) { this.done = true; this.offset = this.totalDistance; }
};
ReelAnim.prototype.draw = function(ctx) {
    var x = GRID_X + this.reel * CELL_W;
    ctx.save();
    ctx.beginPath(); ctx.rect(x, GRID_Y, CELL_W, GRID_H); ctx.clip();
    var startIdx = Math.floor(this.offset / CELL_H);
    for (var r = -1; r <= ROWS; r++) {
        var idx = this.strip.length - ROWS - startIdx + r;
        if (idx < 0 || idx >= this.strip.length) continue;
        var sym = this.strip[idx];
        var subOffset = this.offset % CELL_H;
        var y = GRID_Y + r * CELL_H + subOffset - CELL_H;
        drawSymbol(ctx, sym, x + 2, y + 2, CELL_W - 4, CELL_H - 4);
    }
    ctx.restore();
};

function drawSymbol(ctx, symId, x, y, w, h) {
    if (symId >= 0 && symId < symbolImgs.length && symbolImgs[symId].complete && symbolImgs[symId].naturalWidth > 0) {
        ctx.drawImage(symbolImgs[symId], x, y, w, h);
    } else {
        ctx.fillStyle = 'hsl(' + (symId * 35) + ', 60%, 40%)';
        ctx.fillRect(x, y, w, h);
        ctx.fillStyle = '#fff'; ctx.font = 'bold 14px sans-serif';
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText(String(symId), x + w/2, y + h/2);
    }
}

function emitParticles(x, y, count, color) {
    for (var i = 0; i < count; i++) {
        particles.push({
            x:x, y:y, vx:(Math.random()-0.5)*6, vy:(Math.random()-0.5)*6-2,
            life:0.6+Math.random()*0.8, maxLife:0.6+Math.random()*0.8,
            size:2+Math.random()*4, color:color, alpha:1
        });
    }
}
function updateParticles(dt) {
    for (var i = particles.length - 1; i >= 0; i--) {
        var p = particles[i]; p.x += p.vx; p.y += p.vy; p.vy += 0.08;
        p.life -= dt; p.alpha = Math.max(0, p.life / p.maxLife);
        if (p.life <= 0) particles.splice(i, 1);
    }
}
function drawParticles(ctx) {
    for (var i = 0; i < particles.length; i++) {
        var p = particles[i];
        ctx.globalAlpha = p.alpha; ctx.fillStyle = p.color;
        ctx.beginPath(); ctx.arc(p.x, p.y, p.size, 0, Math.PI*2); ctx.fill();
    }
    ctx.globalAlpha = 1;
}

function drawWinHighlights(ctx, dt) {
    if (winPositions.length === 0) return;
    winFlashTimer += dt;
    var alpha = 0.3 + 0.3 * Math.sin(winFlashTimer * 6);
    ctx.fillStyle = 'rgba(255,215,0,' + alpha + ')';
    for (var i = 0; i < winPositions.length; i++) {
        var wp = winPositions[i];
        ctx.fillRect(GRID_X + wp[0]*CELL_W, GRID_Y + wp[1]*CELL_H, CELL_W, CELL_H);
    }
}

var lastTime = 0;
function frame(ts) {
    var dt = Math.min((ts - lastTime) / 1000, 0.1); lastTime = ts;

    var grad = ctx.createLinearGradient(0, 0, 0, H);
    grad.addColorStop(0, window.THEME.bg0);
    grad.addColorStop(1, window.THEME.bg1);
    ctx.fillStyle = grad; ctx.fillRect(0, 0, W, H);

    ctx.fillStyle = 'rgba(0,0,0,0.3)';
    ctx.fillRect(GRID_X - 4, GRID_Y - 4, GRID_W + 8, GRID_H + 8);
    ctx.strokeStyle = window.THEME.accent; ctx.lineWidth = 2;
    ctx.strokeRect(GRID_X - 4, GRID_Y - 4, GRID_W + 8, GRID_H + 8);

    ctx.strokeStyle = 'rgba(255,255,255,0.06)'; ctx.lineWidth = 1;
    for (var c = 1; c < COLS; c++) {
        ctx.beginPath(); ctx.moveTo(GRID_X + c*CELL_W, GRID_Y); ctx.lineTo(GRID_X + c*CELL_W, GRID_Y + GRID_H); ctx.stroke();
    }
    for (var r = 1; r < ROWS; r++) {
        ctx.beginPath(); ctx.moveTo(GRID_X, GRID_Y + r*CELL_H); ctx.lineTo(GRID_X + GRID_W, GRID_Y + r*CELL_H); ctx.stroke();
    }

    if (gState === 'spinning') {
        var allDone = true;
        for (var i = 0; i < spinReels.length; i++) { spinReels[i].update(dt); spinReels[i].draw(ctx); if (!spinReels[i].done) allDone = false; }
        if (allDone) onSpinComplete();
    } else if (matrix) {
        for (var rr = 0; rr < ROWS; rr++) {
            for (var cc = 0; cc < COLS; cc++) {
                drawSymbol(ctx, matrix[rr][cc], GRID_X + cc*CELL_W + 2, GRID_Y + rr*CELL_H + 2, CELL_W - 4, CELL_H - 4);
            }
        }
        drawWinHighlights(ctx, dt);
    }

    updateParticles(dt); drawParticles(ctx);

    var stats = math.getSessionStats();
    if (stats.totalSpins > 0) {
        ctx.fillStyle = 'rgba(255,255,255,0.2)'; ctx.font = '9px monospace'; ctx.textAlign = 'right';
        ctx.fillText('RTP: ' + stats.sessionRTP.toFixed(1) + '% | Spins: ' + stats.totalSpins + ' | Hit: ' + stats.hitFrequency.toFixed(0) + '%', W - 8, H - 6);
    }

    requestAnimationFrame(frame);
}

function doSpin() {
    if (gState !== 'idle') return;
    var totalBet = bet * lines;
    if (balance < totalBet) { showMsg('INSUFFICIENT BALANCE', '#ef4444'); return; }
    balance -= totalBet; updateUI();
    gState = 'spinning'; winPositions = [];

    var mult = (freeSpinsCtrl && freeSpinsCtrl.isActive) ? freeSpinsCtrl.currentMultiplier : 1;
    var result = math.generateSpinResult();
    matrix = result.matrix;
    lastWin = math.evaluateWins(matrix, bet, totalBet, mult);

    spinReels = [];
    for (var c = 0; c < COLS; c++) {
        var colSyms = [];
        for (var r = 0; r < ROWS; r++) colSyms.push(matrix[r][c]);
        spinReels.push(new ReelAnim(c, colSyms));
    }
    document.getElementById('btn-spin').textContent = '...';
    document.getElementById('btn-spin').classList.add('spinning');
}

function onSpinComplete() {
    gState = 'idle';
    document.getElementById('btn-spin').textContent = 'SPIN';
    document.getElementById('btn-spin').classList.remove('spinning');
    if (!lastWin) return;
    totalWinAmt = lastWin.totalWin;

    if (totalWinAmt > 0) {
        balance += totalWinAmt; updateUI();
        var lws = lastWin.lineWinAmounts || [];
        for (var i = 0; i < lws.length; i++) {
            var ws = lws[i].wonSymbols || [];
            for (var j = 0; j < ws.length; j++) winPositions.push(ws[j]);
        }
        for (var k = 0; k < winPositions.length; k++) {
            emitParticles(GRID_X + winPositions[k][0]*CELL_W + CELL_W/2,
                         GRID_Y + winPositions[k][1]*CELL_H + CELL_H/2,
                         8, totalWinAmt >= bet*lines*15 ? '#FFD700' : '#22c55e');
        }
        showMsg('+' + totalWinAmt.toFixed(2), totalWinAmt >= bet*lines*15 ? '#FFD700' : '#22c55e');
        addHistory(totalWinAmt, true);
    } else {
        addHistory(0, false);
    }

    if (lastWin.freeSpinsAwarded > 0 && freeSpinsCtrl) {
        freeSpinsCtrl.trigger(lastWin.freeSpinsAwarded);
        showMsg('FREE SPINS! ' + lastWin.freeSpinsAwarded + ' spins', '#FF00FF');
    }
    if (autoplay && balance >= bet * lines) { autoTimer = setTimeout(doSpin, 600); }
}

function updateUI() {
    document.getElementById('ctrl-balance').textContent = balance.toFixed(2);
    document.getElementById('ctrl-bet').textContent = (bet * lines).toFixed(2);
}

function showMsg(text, color) {
    var el = document.getElementById('win-msg');
    el.textContent = text; el.style.color = color;
    el.classList.add('show');
    setTimeout(function(){ el.classList.remove('show'); }, 2000);
}

function addHistory(amount, won) {
    var list = document.getElementById('hist-list');
    if (!list) return;
    var row = document.createElement('div'); row.className = 'hist-row';
    row.innerHTML = '<span class="m ' + (won?'w':'l') + '">' + (won ? '+'+amount.toFixed(2) : 'No win') + '</span>';
    list.prepend(row);
    if (list.children.length > 15) list.lastChild.remove();
}

function changeBet(dir) {
    var bets = CFG.betConfig ? CFG.betConfig.bets : [1,2,5,10,20,50];
    var idx = bets.indexOf(bet) + dir;
    idx = Math.max(0, Math.min(bets.length - 1, idx));
    bet = bets[idx]; updateUI();
}

function toggleAuto() {
    autoplay = !autoplay;
    document.getElementById('btn-auto').style.color = autoplay ? window.THEME.accent : '#aaa';
    if (autoplay && gState === 'idle') doSpin();
    if (!autoplay && autoTimer) clearTimeout(autoTimer);
}

window.doSpin = doSpin;
window.changeBet = changeBet;
window.toggleAuto = toggleAuto;
window.togglePaytable = function() { document.getElementById('paytable-overlay').classList.toggle('show'); };

// Build paytable grid
var ptGrid = document.getElementById('paytable-grid');
if (ptGrid && CFG.paytable) {
    var symNames = CFG.symbolWeights || {};
    for (var si = 0; si < symbolImgs.length; si++) {
        var card = document.createElement('div'); card.className = 'pt-card';
        var sname = symNames[String(si)] ? symNames[String(si)].name : ('Symbol '+si);
        var pays = '';
        for (var m = 5; m >= 2; m--) {
            var val = CFG.paytable[si+'-'+m];
            if (val) pays += m + '\u00d7 <b>' + val + '</b><br>';
        }
        if (!pays) pays = '<span style="opacity:0.4">Special</span>';
        card.innerHTML = '<img src="' + SYMBOLS[si] + '" width="48" height="48" alt="' + sname + '"><div class="pt-name">' + sname + '</div><div class="pt-pays">' + pays + '</div>';
        ptGrid.appendChild(card);
    }
}

updateUI();
requestAnimationFrame(frame);
})();

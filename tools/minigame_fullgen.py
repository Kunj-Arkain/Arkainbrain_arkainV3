"""
ARKAINBRAIN — Full Game Code Generation Engine

Instead of skinning templates, this generates UNIQUE game code per run.

Architecture:
  1. GAME SDK (~250 lines) — reliable boilerplate injected into every game:
     math utils, easing, tween manager, particle system, canvas setup,
     balance/bet management, history, responsive layout, dark theme CSS

  2. LLM CODEGEN — GPT writes the game-specific code:
     state machine, rendering, animations, interactions, UI

  3. VALIDATION — automated checks for common LLM codegen issues:
     missing functions, infinite loops, syntax errors, RTP verification

  4. FIX PASS — if validation fails, LLM gets the errors and fixes them

Usage:
    from tools.minigame_fullgen import FullGameGenerator
    gen = FullGameGenerator()
    result = gen.generate(
        description="A deep sea diving game where you descend for treasure",
        game_type="crash",        # archetype hint (optional)
        target_rtp=96.0,
        house_edge=0.04,
        theme="underwater",
        volatility="medium",
    )
    # result.html — full playable game
    # result.validation — validation report
    # result.attempts — how many generate/fix cycles
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("arkainbrain.fullgen")


# ═══════════════════════════════════════════════════════════════
# Game SDK — injected into every generated game
# ═══════════════════════════════════════════════════════════════

GAME_SDK_CSS = r"""
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
:root{--acc:ACCENT;--acc2:ACCENT2;--bg0:BG0;--bg1:BG1;--txt:TXT;--dim:DIM;--win:WIN;--lose:LOSE;--gold:GOLD}
body{font-family:'Inter',sans-serif;background:linear-gradient(135deg,var(--bg0),var(--bg1));color:var(--txt);min-height:100vh;display:flex;flex-direction:column;overflow:hidden;-webkit-user-select:none;user-select:none}
.hdr{text-align:center;padding:14px 12px 6px;position:relative;z-index:5}
.hdr h1{font-size:20px;font-weight:800;background:linear-gradient(135deg,var(--acc),var(--acc2));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.hdr .sub{font-size:10px;color:var(--dim);margin-top:3px;letter-spacing:1.5px;text-transform:uppercase}
.stats{display:flex;justify-content:space-around;padding:8px 16px;font-size:12px;background:rgba(255,255,255,0.02);border-top:1px solid rgba(255,255,255,0.05);border-bottom:1px solid rgba(255,255,255,0.05);position:relative;z-index:5}
.stats .lbl{color:var(--dim)}.stats .val{font-weight:700;color:var(--acc2);font-variant-numeric:tabular-nums}
.stats .val.neg{color:var(--lose)}
#game-area{flex:1;position:relative;min-height:280px;overflow:hidden}
#game-canvas{position:absolute;inset:0;width:100%;height:100%}
.ctrls{padding:10px 16px;display:flex;flex-direction:column;gap:8px;background:rgba(255,255,255,0.02);border-top:1px solid rgba(255,255,255,0.05);position:relative;z-index:5}
.bet-row{display:flex;gap:4px;flex-wrap:wrap}
.bet-btn{padding:6px 12px;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:var(--txt);font-size:11px;font-weight:600;cursor:pointer;transition:all .15s}
.bet-btn:hover{border-color:var(--acc)}.bet-btn.active{background:var(--acc);border-color:var(--acc);color:#fff;box-shadow:0 0 12px rgba(99,102,241,0.3)}
.play-btn{width:100%;padding:14px;border-radius:10px;border:none;background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff;font-size:15px;font-weight:700;cursor:pointer;letter-spacing:1px;text-transform:uppercase;transition:all .2s;position:relative;overflow:hidden}
.play-btn:hover{box-shadow:0 4px 20px rgba(99,102,241,0.3);transform:translateY(-1px)}.play-btn:active{transform:translateY(1px)}
.play-btn:disabled{opacity:.4;cursor:not-allowed;transform:none}
.play-btn.cashout{background:linear-gradient(135deg,var(--gold),var(--win))}
.play-btn.cashout::after{content:'';position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,0.15),transparent);animation:sheen 1.5s infinite}
@keyframes sheen{0%{transform:translateX(-100%)}100%{transform:translateX(100%)}}
.result-toast{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%) scale(0);font-size:28px;font-weight:800;z-index:50;pointer-events:none;text-shadow:0 4px 20px rgba(0,0,0,0.5);transition:transform .3s cubic-bezier(0.34,1.56,0.64,1),opacity .3s}
.result-toast.show{transform:translate(-50%,-50%) scale(1);opacity:1}
.result-toast.hide{transform:translate(-50%,-50%) scale(0.8);opacity:0}
.hist{padding:6px 16px 10px;max-height:65px;overflow-y:auto;position:relative;z-index:5}
.hist h3{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:1px;margin-bottom:3px}
.hist-row{display:flex;gap:8px;font-size:10px;padding:2px 0;border-bottom:1px solid rgba(255,255,255,0.03)}
.hist-row .m{font-weight:700;min-width:50px}.hist-row .w{color:var(--win)}.hist-row .l{color:var(--lose)}
.float-num{position:absolute;pointer-events:none;font-weight:800;z-index:20;animation:floatUp 1.2s ease-out forwards}
@keyframes floatUp{0%{opacity:1;transform:translateY(0) scale(.8)}20%{transform:translateY(-10px) scale(1.1)}100%{opacity:0;transform:translateY(-60px) scale(.9)}}
"""

GAME_SDK_JS = r"""
"use strict";

// ═══════════════════════════════════════════════════════════
// § ARKAIN GAME SDK — v2
// Math, easing, particles, drawing helpers, balance, history
// ═══════════════════════════════════════════════════════════

const lerp=(a,b,t)=>a+(b-a)*t, clamp=(v,lo,hi)=>v<lo?lo:v>hi?hi:v;
const rng=(a,b)=>a+Math.random()*(b-a), rngI=(a,b)=>Math.floor(rng(a,b+1));
const TAU=Math.PI*2;
function hexRgb(h){const n=parseInt(h.replace('#',''),16);return[(n>>16)&255,(n>>8)&255,n&255]}
function rgba(hex,a){const[r,g,b]=hexRgb(hex);return`rgba(${r},${g},${b},${a})`}
function lerpColor(c1,c2,t){const a=hexRgb(c1),b=hexRgb(c2);return`rgb(${Math.round(lerp(a[0],b[0],t))},${Math.round(lerp(a[1],b[1],t))},${Math.round(lerp(a[2],b[2],t))})`}

const Ease={
  linear:t=>t, inQuad:t=>t*t, outQuad:t=>t*(2-t),
  inOutQuad:t=>t<.5?2*t*t:1-Math.pow(-2*t+2,2)/2,
  outCubic:t=>(--t)*t*t+1, outExpo:t=>t===1?1:1-Math.pow(2,-10*t),
  outElastic:t=>t===0||t===1?t:Math.pow(2,-10*t)*Math.sin((t-.1)*5*Math.PI)+1,
  outBounce:t=>{if(t<1/2.75)return 7.5625*t*t;if(t<2/2.75){t-=1.5/2.75;return 7.5625*t*t+.75}if(t<2.5/2.75){t-=2.25/2.75;return 7.5625*t*t+.9375}t-=2.625/2.75;return 7.5625*t*t+.984375},
  outBack:t=>{const s=1.7;return(t-=1)*t*((s+1)*t+s)+1},
};

// ── Tween Manager ──
const TW={_t:[],
  add(obj,props,dur,ease='outCubic',onDone){
    const e=typeof ease==='function'?ease:Ease[ease]||Ease.outCubic;
    const start={};for(const k in props)start[k]=obj[k]||0;
    this._t.push({obj,start,end:props,dur,e,elapsed:0,onDone});
  },
  update(dt){
    for(let i=this._t.length-1;i>=0;i--){
      const tw=this._t[i];tw.elapsed+=dt;
      const t=clamp(tw.elapsed/tw.dur,0,1),v=tw.e(t);
      for(const k in tw.end)tw.obj[k]=lerp(tw.start[k],tw.end[k],v);
      if(t>=1){if(tw.onDone)tw.onDone();this._t.splice(i,1)}
    }
  }
};

// ── Particle System ──
class Particles{
  constructor(max=200){this.pool=[];this.max=max}
  emit(x,y,count,color,opts={}){
    for(let i=0;i<count&&this.pool.length<this.max;i++){
      this.pool.push({x,y,vx:rng(-2,2)*(opts.spread||1),vy:rng(-3,-0.5)*(opts.speed||1),
        life:rng(0.5,1.5)*(opts.life||1),maxLife:rng(0.5,1.5)*(opts.life||1),
        size:rng(1,4)*(opts.size||1),color,alpha:1,rot:rng(0,TAU),
        shape:opts.shape||'circle'});
    }
  }
  burst(x,y,count,colors,opts={}){
    const c=Array.isArray(colors)?colors:[colors];
    for(let i=0;i<count;i++){
      const angle=rng(0,TAU),speed=rng(1,5)*(opts.speed||1);
      this.pool.push({x,y,vx:Math.cos(angle)*speed,vy:Math.sin(angle)*speed,
        life:rng(0.4,1.2),maxLife:rng(0.4,1.2),size:rng(2,5)*(opts.size||1),
        color:c[i%c.length],alpha:1,rot:rng(0,TAU),shape:opts.shape||'circle'});
      if(this.pool.length>=this.max)break;
    }
  }
  update(dt){
    for(let i=this.pool.length-1;i>=0;i--){
      const p=this.pool[i];p.x+=p.vx;p.y+=p.vy;p.vy+=0.05;p.life-=dt;
      p.alpha=clamp(p.life/p.maxLife,0,1);p.rot+=dt;
      if(p.life<=0)this.pool.splice(i,1);
    }
  }
  draw(ctx){
    for(const p of this.pool){
      ctx.globalAlpha=p.alpha;ctx.fillStyle=p.color;
      if(p.shape==='square'){
        ctx.save();ctx.translate(p.x,p.y);ctx.rotate(p.rot);
        ctx.fillRect(-p.size/2,-p.size/2,p.size,p.size);ctx.restore();
      } else {
        ctx.beginPath();ctx.arc(p.x,p.y,p.size,0,TAU);ctx.fill();
      }
    }
    ctx.globalAlpha=1;
  }
  get count(){return this.pool.length}
}

// ── Screen Shake ──
const Shake={_x:0,_y:0,_intensity:0,_decay:0.92,
  trigger(intensity=8){this._intensity=intensity},
  update(){
    if(this._intensity<0.5){this._x=0;this._y=0;return;}
    this._x=rng(-this._intensity,this._intensity);
    this._y=rng(-this._intensity,this._intensity);
    this._intensity*=this._decay;
  },
  apply(ctx){if(this._x||this._y)ctx.translate(this._x,this._y)}
};

// ── Theme Color Access ──
function themeColor(name){
  return getComputedStyle(document.documentElement).getPropertyValue('--'+name).trim();
}
// Shortcut: const acc=themeColor('acc'), win=themeColor('win'), etc.

// ── Drawing Helpers ──
function drawCenteredText(ctx,text,x,y,size,color,font){
  ctx.save();ctx.font=(font?`${size}px '${font}'`:`800 ${size}px Inter`);
  ctx.fillStyle=color;ctx.textAlign='center';ctx.textBaseline='middle';
  ctx.fillText(text,x,y);ctx.restore();
}
function drawGlowText(ctx,text,x,y,size,color,glowColor,blur){
  ctx.save();ctx.shadowColor=glowColor||color;ctx.shadowBlur=blur||20;
  drawCenteredText(ctx,text,x,y,size,color);ctx.restore();
}
function drawRoundRect(ctx,x,y,w,h,r,fill,stroke){
  ctx.beginPath();ctx.moveTo(x+r,y);ctx.lineTo(x+w-r,y);ctx.quadraticCurveTo(x+w,y,x+w,y+r);
  ctx.lineTo(x+w,y+h-r);ctx.quadraticCurveTo(x+w,y+h,x+w-r,y+h);ctx.lineTo(x+r,y+h);
  ctx.quadraticCurveTo(x,y+h,x,y+h-r);ctx.lineTo(x,y+r);ctx.quadraticCurveTo(x,y,x+r,y);
  ctx.closePath();if(fill){ctx.fillStyle=fill;ctx.fill()}if(stroke){ctx.strokeStyle=stroke;ctx.stroke()}
}
function drawProgressBar(ctx,x,y,w,h,pct,fillColor,bgColor,r){
  r=r||h/2;drawRoundRect(ctx,x,y,w,h,r,bgColor||'rgba(255,255,255,0.1)');
  if(pct>0)drawRoundRect(ctx,x,y,w*clamp(pct,0,1),h,r,fillColor);
}
function drawGrid(ctx,cols,rows,cellW,cellH,startX,startY,gap,drawCell){
  for(let r=0;r<rows;r++)for(let c=0;c<cols;c++){
    const x=startX+c*(cellW+gap),y=startY+r*(cellH+gap);
    drawCell(ctx,x,y,cellW,cellH,r*cols+c,r,c);
  }
}
function clearCanvas(){
  const bg0=themeColor('bg0')||'#030014',bg1=themeColor('bg1')||'#0a0020';
  const grad=ctx.createLinearGradient(0,0,0,H);
  grad.addColorStop(0,bg0);grad.addColorStop(1,bg1);ctx.fillStyle=grad;ctx.fillRect(0,0,W,H);
}

// ── Balance & Bet Management ──
let balance=STARTING_BALANCE, currentBet=BET_OPTIONS[0], profit=0;
const BET_OPTS=BET_OPTIONS;

function updateUI(){
  const bEl=document.getElementById('s-bal'),pEl=document.getElementById('s-pft'),betEl=document.getElementById('s-bet');
  if(bEl)bEl.textContent='$'+balance.toFixed(2);
  if(pEl){pEl.textContent=(profit>=0?'+$':'-$')+Math.abs(profit).toFixed(2);pEl.className='val'+(profit<0?' neg':'');}
  if(betEl)betEl.textContent='$'+currentBet.toFixed(2);
}
function win(mult){
  const amt=currentBet*mult;balance+=amt;profit+=amt;updateUI();
  addHistory(mult,true,currentBet);
}
function lose(){
  balance-=currentBet;profit-=currentBet;updateUI();
  addHistory(0,false,currentBet);
}
function canBet(){return balance>=currentBet}

function initBetButtons(){
  const row=document.getElementById('bet-row');if(!row)return;
  BET_OPTS.forEach((v,i)=>{
    const b=document.createElement('button');b.className='bet-btn'+(i===0?' active':'');
    b.textContent='$'+v.toFixed(2);b.onclick=()=>{
      if(typeof gameState!=='undefined'&&gameState!=='idle')return;
      currentBet=v;row.querySelectorAll('.bet-btn').forEach(x=>x.classList.remove('active'));
      b.classList.add('active');updateUI();
    };row.appendChild(b);
  });
}

function addHistory(mult,won,betAmt){
  const list=document.getElementById('hist-list');if(!list)return;
  const row=document.createElement('div');row.className='hist-row';
  row.innerHTML=`<span class="m ${won?'w':'l'}">${mult.toFixed(2)}x</span><span>${won?'+':'-'}$${(won?betAmt*mult:betAmt).toFixed(2)}</span>`;
  list.prepend(row);if(list.children.length>20)list.lastChild.remove();
}

function showToast(text,color,duration=1500){
  let toast=document.getElementById('result-toast');
  if(!toast){toast=document.createElement('div');toast.id='result-toast';toast.className='result-toast';document.body.appendChild(toast);}
  toast.textContent=text;toast.style.color=color;toast.className='result-toast show';
  setTimeout(()=>toast.className='result-toast hide',duration);
}

function floatNumber(x,y,text,color){
  const el=document.createElement('div');el.className='float-num';el.textContent=text;
  el.style.cssText=`left:${x}px;top:${y}px;color:${color};font-size:16px`;
  document.getElementById('game-area').appendChild(el);
  setTimeout(()=>el.remove(),1200);
}

function setPlayBtn(text,cssClass){
  const btn=document.getElementById('play-btn');if(!btn)return;
  btn.textContent=text;btn.className='play-btn'+(cssClass?' '+cssClass:'');
}

// ── Canvas Setup ──
let canvas,ctx,W,H;
function initCanvas(){
  canvas=document.getElementById('game-canvas');ctx=canvas.getContext('2d');
  function resize(){
    const r=devicePixelRatio||1,c=canvas.parentElement;
    W=c.clientWidth;H=c.clientHeight;canvas.width=W*r;canvas.height=H*r;
    ctx.setTransform(r,0,0,r,0,0);
  }
  resize();window.addEventListener('resize',resize);
}

// ── Provably Fair RNG ──
function generateCrashPoint(houseEdge){
  const r=Math.random();
  if(r<houseEdge)return 1.0;
  return Math.floor(100/(r*100))/100*((100-houseEdge*100)/100);
}
function generateOutcome(winChance){return Math.random()<winChance}

// ── Frame Loop ──
let _lastTime=0;
function startGameLoop(renderFn){
  function frame(ts){
    const dt=Math.min((ts-_lastTime)/1000,0.1);_lastTime=ts;
    TW.update(dt);Shake.update();
    renderFn(dt,ts);
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

// Init
document.addEventListener('DOMContentLoaded',()=>{
  initCanvas();initBetButtons();updateUI();
  if(typeof initGame==='function')initGame();
  if(typeof gameRender==='function')startGameLoop(gameRender);
});
"""


# ═══════════════════════════════════════════════════════════════
# Code Generation
# ═══════════════════════════════════════════════════════════════

@dataclass
class GenResult:
    html: str
    validation: dict
    attempts: int
    design: dict
    game_code_lines: int

    def to_dict(self):
        return {
            "validation": self.validation,
            "attempts": self.attempts,
            "game_code_lines": self.game_code_lines,
            "html_length": len(self.html),
        }


def _get_model() -> str:
    try:
        from config.settings import LLMConfig
        m = LLMConfig.get_llm("game_designer")
        return m.replace("openai/", "") if m else "gpt-4.1"
    except Exception:
        return os.getenv("LLM_HEAVY", "gpt-4.1")


def _build_generation_prompt(description: str, design: dict, config: dict) -> str:
    """Build the prompt that generates game-specific code."""

    game_type = config.get("game_type", "custom")
    house_edge = config.get("house_edge", 0.04)
    target_rtp = config.get("target_rtp", 96.0)
    max_mult = config.get("max_multiplier", 100)
    volatility = config.get("volatility", "medium")

    flavor = design.get("flavor_text", {})
    win_msgs = json.dumps(flavor.get("win_messages", ["Nice win!", "Winner!"]))
    loss_msgs = json.dumps(flavor.get("loss_messages", ["Try again!", "Next round!"]))
    big_win_msgs = json.dumps(flavor.get("big_win_messages", ["MEGA WIN!", "JACKPOT!"]))
    labels = design.get("logic", {}).get("game_labels", {})
    effects = design.get("logic", {}).get("visual_effects", {})

    return f"""You are an expert HTML5 game developer. Write the GAME-SPECIFIC JavaScript code for a unique casino mini-game.

## GAME CONCEPT
{description}

Title: {design.get('title', 'Custom Game')}
Theme: {design.get('sound_theme', 'casino')}
Archetype hint: {game_type}

## MATH REQUIREMENTS
- House edge: {house_edge*100:.1f}%
- Target RTP: {target_rtp:.1f}%
- Max multiplier: {max_mult}x
- Volatility: {volatility}

## THEME DATA
- Win messages: {win_msgs}
- Loss messages: {loss_msgs}
- Big win messages: {big_win_msgs}
- Play button label: "{labels.get('play_button', 'PLAY')}"
- Cashout button label: "{labels.get('cashout_button', 'CASH OUT')}"
- Particle type: {effects.get('particle_type', 'stars')}

## SDK ALREADY PROVIDED (do NOT redefine these)
The following are already defined and available:
- `canvas`, `ctx`, `W`, `H` — canvas and 2D context, auto-resized dimensions
- `lerp(a,b,t)`, `clamp(v,lo,hi)`, `rng(a,b)`, `rngI(a,b)`, `TAU` — math
- `hexRgb(hex)`, `rgba(hex,a)`, `lerpColor(c1,c2,t)` — color utilities
- `Ease` — easing: outCubic, outElastic, outBounce, outBack, outExpo, linear, etc.
- `TW` — tween: `TW.add(obj, {{x:100,y:200}}, 0.5, 'outElastic', onDone)`
- `Particles` — system: `new Particles(200)`, `.emit(x,y,count,color,opts)`, `.burst(x,y,count,colorsArray,opts)`, `.update(dt)`, `.draw(ctx)`, `.count`
  - opts: `{{spread:1, speed:1, life:1, size:1, shape:'circle'|'square'}}`
- `Shake` — screen shake: `Shake.trigger(8)`, `Shake.update()`, `Shake.apply(ctx)` (update/apply called by SDK loop)
- `themeColor(name)` — read CSS var: `themeColor('acc')`, `themeColor('win')`, `themeColor('lose')`
- `clearCanvas()` — fills canvas with theme gradient background
- `drawCenteredText(ctx,text,x,y,size,color,fontName)` — centered bold text
- `drawGlowText(ctx,text,x,y,size,color,glowColor,blur)` — text with glow effect
- `drawRoundRect(ctx,x,y,w,h,radius,fillColor,strokeColor)` — rounded rectangle
- `drawProgressBar(ctx,x,y,w,h,pct,fillColor,bgColor,radius)` — progress/meter bar
- `drawGrid(ctx,cols,rows,cellW,cellH,startX,startY,gap,drawCellFn)` — calls drawCellFn(ctx,x,y,w,h,index,row,col) for each cell
- `balance`, `currentBet`, `profit` — economy state
- `canBet()` — returns true if player can afford currentBet
- `win(multiplier)` — awards win: updates balance, profit, history
- `lose()` — deducts bet: updates balance, profit, history
- `updateUI()` — refreshes balance/bet/profit stats display
- `setPlayBtn(text, cssClass)` — set play button label and class ('cashout' for gold button)
- `addHistory(mult, won, betAmt)` — manually add to history (win/lose do this automatically)
- `showToast(text, color, duration)` — big centered floating text
- `floatNumber(x, y, text, color)` — small floating number at position
- `generateCrashPoint(houseEdge)` — returns a multiplier with correct house edge
- `generateOutcome(winChance)` — returns true/false with given probability

## YOUR TASK
Write ONLY the game-specific code. You MUST define these two functions:

1. `function initGame()` — called once on load. Set up game state, create objects.
2. `function gameRender(dt, timestamp)` — called every frame. Update + draw everything.

You should also define:
3. `function onPlayClick()` — called when the play button is clicked.

Additional requirements:
- Define a `gameState` variable ('idle', 'playing', 'result') — the SDK checks this.
- Use `canvas`/`ctx` for rendering. Call `clearCanvas()` at the start of each frame.
- After clearCanvas(), call `ctx.save(); Shake.apply(ctx);` then draw, then `ctx.restore();`
- Use `Particles` for visual effects. Call `.burst()` for explosions, `.emit()` for continuous.
- Use `TW.add()` for smooth animations on any object.
- Use `Shake.trigger(intensity)` on big wins or crashes.
- Use `win(mult)` and `lose()` for economy — they handle balance + history automatically.
- Use `canBet()` before starting a round.
- Use `setPlayBtn(text, class)` to toggle button between play/cashout states.
- Use `drawGlowText()` for the main multiplier/score display.
- Use `drawProgressBar()` for meters, gauges, health bars, etc.
- Use `drawGrid()` for tile-based games (mines, scratch, memory).
- Use `themeColor('acc')`, `themeColor('win')`, etc. to read theme colors.
- Use `showToast()` for win/loss messages from the theme arrays above.
- The math must produce approximately {target_rtp:.0f}% RTP over many rounds.
- Make it feel polished: smooth animations, color transitions, screen shake on big wins.
- The game canvas should use the full available area creatively.
- DO NOT call initCanvas, initBetButtons, updateUI, or startGameLoop — they're called automatically.

## CODING PATTERN (follow this structure)
```
let gameState='idle';
const particles=new Particles(150);
// ... game-specific state ...

function initGame(){{ /* one-time setup */ }}

function onPlayClick(){{
  if(gameState==='idle' && canBet()){{ /* start round */ }}
  else if(gameState==='playing'){{ /* cashout */ }}
}}

function gameRender(dt, timestamp){{
  clearCanvas();
  ctx.save(); Shake.apply(ctx);
  // ... draw game visuals ...
  particles.update(dt); particles.draw(ctx);
  ctx.restore();
}}
```

## OUTPUT FORMAT
Return ONLY raw JavaScript code. No markdown fences, no explanations.
Start directly with `// Game: {design.get('title', 'Custom Game')}` and write the code.
Target: 150-350 lines of game-specific code."""


def _validate_game_code(code: str) -> dict:
    """Validate LLM-generated game code for common issues."""
    issues = []
    warnings = []
    score = 100

    # Required functions
    for fn in ["initGame", "gameRender", "onPlayClick"]:
        if f"function {fn}" not in code and f"{fn}=" not in code:
            issues.append(f"Missing required function: {fn}")
            score -= 20

    # Must reference gameState
    if "gameState" not in code:
        issues.append("Missing gameState variable")
        score -= 15

    # Must use canvas
    if "ctx." not in code:
        issues.append("No canvas rendering (ctx. not found)")
        score -= 20

    # Check for redefined SDK functions (common LLM mistake)
    sdk_fns = ["lerp", "clamp", "rng", "hexRgb", "rgba", "updateUI",
               "initCanvas", "initBetButtons", "startGameLoop", "addHistory",
               "clearCanvas", "drawCenteredText", "drawGlowText", "drawRoundRect",
               "drawProgressBar", "themeColor", "canBet", "setPlayBtn"]
    for fn in sdk_fns:
        if re.search(rf'\b(function\s+{fn}\b|const\s+{fn}\s*=)', code):
            warnings.append(f"Redefines SDK function: {fn} (will be stripped)")

    # Check for SDK class redefinitions
    for cls in ["Particles", "Shake", "TW", "Ease"]:
        if re.search(rf'\b(class\s+{cls}\b|const\s+{cls}\s*=)', code):
            warnings.append(f"Redefines SDK class: {cls} (will be stripped)")

    # Check for balance manipulation via SDK helpers
    uses_win_lose = "win(" in code or "lose()" in code
    uses_manual = "balance" in code and ("updateUI" in code or "addHistory" in code)
    if not uses_win_lose and not uses_manual:
        warnings.append("Never calls win()/lose() — game won't affect economy")

    # Check for obvious infinite loops
    if re.search(r'while\s*\(\s*true\s*\)', code):
        issues.append("Contains while(true) — potential infinite loop")
        score -= 25

    # Check for canvas clearing
    if "clearCanvas" not in code and "clearRect" not in code and "fillRect" not in code:
        warnings.append("No canvas clearing — may cause visual artifacts")

    # Check for visual richness
    has_particles = "particles" in code.lower() or "Particles" in code
    has_tweens = "TW.add" in code
    has_toast = "showToast" in code
    has_shake = "Shake.trigger" in code
    has_glow = "drawGlowText" in code or "drawCentered" in code

    richness = sum([has_particles, has_tweens, has_toast, has_shake, has_glow])
    if richness < 2:
        warnings.append(f"Low visual richness ({richness}/5 SDK features used)")
        score -= 5

    # Brace matching
    open_braces = code.count("{")
    close_braces = code.count("}")
    if abs(open_braces - close_braces) > 2:
        issues.append(f"Brace mismatch: {open_braces} open, {close_braces} close")
        score -= 15

    # Check code isn't too short (likely incomplete)
    lines = code.count("\n") + 1
    if lines < 50:
        issues.append(f"Code too short ({lines} lines) — likely incomplete")
        score -= 20
    elif lines < 100:
        warnings.append(f"Code is short ({lines} lines) — may lack polish")

    return {
        "passed": len(issues) == 0,
        "score": max(0, score),
        "issues": issues,
        "warnings": warnings,
        "lines": lines,
        "has_particles": has_particles,
        "has_tweens": has_tweens,
        "has_shake": has_shake,
        "visual_richness": richness,
    }


def _strip_sdk_redefinitions(code: str) -> str:
    """Remove any SDK functions the LLM accidentally redefined."""
    sdk_fns = [
        "lerp", "clamp", "rng", "rngI", "hexRgb", "rgba", "lerpColor",
        "updateUI", "initCanvas", "initBetButtons", "startGameLoop",
        "addHistory", "showToast", "floatNumber", "generateCrashPoint",
        "generateOutcome", "clearCanvas", "drawCenteredText", "drawGlowText",
        "drawRoundRect", "drawProgressBar", "drawGrid", "themeColor",
        "setPlayBtn", "canBet",
    ]
    # These are game functions the LLM SHOULD define
    game_fns = {"initGame", "gameRender", "onPlayClick"}

    for fn in sdk_fns:
        if fn in game_fns:
            continue
        # Remove `function fnName(...){...}` — careful with multi-line
        # Use a simpler approach: just comment out the line
        code = re.sub(
            rf'^(function\s+{fn}\b.*)',
            rf'// [SDK] stripped duplicate: {fn}',
            code,
            flags=re.MULTILINE,
        )
        code = re.sub(
            rf'^(const\s+{fn}\s*=.*)',
            rf'// [SDK] stripped duplicate: {fn}',
            code,
            flags=re.MULTILINE,
        )

    # Also strip class redefinitions
    for cls in ["Particles", "Shake", "TW", "Ease"]:
        code = re.sub(
            rf'^(const\s+{cls}\s*=|class\s+{cls}\b)',
            rf'// [SDK] stripped duplicate class: {cls}',
            code,
            flags=re.MULTILINE,
        )

    return code


def _assemble_html(design: dict, config: dict, game_code: str) -> str:
    """Assemble the final HTML from SDK + game code."""
    ui = design.get("ui_theme", {})
    logic = design.get("logic", {})
    labels = logic.get("game_labels", {})

    # Apply theme colors to SDK CSS
    css = GAME_SDK_CSS
    replacements = {
        "ACCENT": ui.get("primary_color", "#6366f1"),
        "ACCENT2": ui.get("secondary_color", "#06b6d4"),
        "BG0": ui.get("bg_start", "#030014"),
        "BG1": ui.get("bg_end", "#0a0020"),
        "TXT": ui.get("text_color", "#e2e8f0"),
        "DIM": ui.get("text_dim", "#64748b"),
        "WIN": ui.get("win_color", "#22c55e"),
        "LOSE": ui.get("lose_color", "#ef4444"),
        "GOLD": ui.get("gold_color", "#f59e0b"),
    }
    for token, value in replacements.items():
        css = css.replace(token, value)

    # Build SDK JS with config
    bet_opts = logic.get("bet_options", [0.10, 0.25, 0.50, 1.00, 2.00, 5.00, 10.00])
    sdk_js = GAME_SDK_JS.replace(
        "STARTING_BALANCE", str(config.get("starting_balance", 1000))
    ).replace(
        "BET_OPTIONS", json.dumps(bet_opts)
    )

    title = design.get("title", "Arkain Game")
    subtitle = design.get("subtitle", design.get("tagline", ""))
    icon = design.get("icon", "🎮")
    play_label = labels.get("play_button", "PLAY")

    # Optional title font
    title_font = ui.get("title_font", "")
    font_link = ""
    font_css = ""
    if title_font and title_font != "Inter":
        safe_font = title_font.replace("'", "").replace('"', '')
        font_link = f'<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family={safe_font.replace(" ", "+")}:wght@400;700;800&display=swap">'
        font_css = f".hdr h1{{font-family:'{safe_font}',sans-serif}}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
<title>ARCADE — {title}</title>
{font_link}
<style>
{css}
{font_css}
</style>
</head>
<body>

<div class="hdr">
  <h1>{icon} {title.upper()}</h1>
  <div class="sub">{subtitle}</div>
</div>
<div class="stats">
  <div><span class="lbl">Balance </span><span class="val" id="s-bal">$1,000.00</span></div>
  <div><span class="lbl">Bet </span><span class="val" id="s-bet">$1.00</span></div>
  <div><span class="lbl">Profit </span><span class="val" id="s-pft">$0.00</span></div>
</div>
<div id="game-area"><canvas id="game-canvas"></canvas></div>
<div class="ctrls">
  <div class="bet-row" id="bet-row"></div>
  <button class="play-btn" id="play-btn" onclick="onPlayClick()">{play_label}</button>
</div>
<div class="hist"><h3>History</h3><div id="hist-list"></div></div>

<script>
{sdk_js}
</script>

<script>
{game_code}
</script>

</body>
</html>"""


def _generate_fix_prompt(code: str, validation: dict) -> str:
    """Build a prompt to fix issues found in validation."""
    return f"""The following game code has issues that need fixing.

## ISSUES (must fix):
{json.dumps(validation['issues'], indent=2)}

## WARNINGS (should fix if possible):
{json.dumps(validation['warnings'], indent=2)}

## CURRENT CODE:
```javascript
{code}
```

Fix all issues. Return ONLY the corrected JavaScript code, no markdown fences.
Remember: the SDK provides lerp, clamp, rng, Ease, TW, Particles, balance, currentBet,
updateUI, addHistory, showToast, floatNumber, canvas, ctx, W, H, etc.
Do NOT redefine SDK functions."""


class FullGameGenerator:
    """Generates fully unique HTML5 games via LLM code generation."""

    def __init__(self, max_fix_attempts: int = 2):
        self.max_fix_attempts = max_fix_attempts

    def generate(
        self,
        description: str,
        design: dict,
        config: dict,
    ) -> GenResult:
        """Generate a complete, unique HTML5 game.

        Args:
            description: Natural language game concept
            design: Full design dict from _generate_game_design (colors, theme, flavor text)
            config: Game config (house_edge, target_rtp, max_multiplier, etc.)

        Returns: GenResult with html, validation report, attempt count
        """
        import openai
        client = openai.OpenAI()
        model = _get_model()

        # Pass 1: Generate game code
        prompt = _build_generation_prompt(description, design, config)
        logger.info(f"Generating game code for: {design.get('title', description[:50])}")

        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
            temperature=0.85,
        )
        code = resp.choices[0].message.content.strip()
        if code.startswith("```"):
            code = code.split("\n", 1)[1].rsplit("```", 1)[0]

        # Clean up SDK redefinitions
        code = _strip_sdk_redefinitions(code)

        # Validate
        validation = _validate_game_code(code)
        attempts = 1

        # Fix loop
        while not validation["passed"] and attempts <= self.max_fix_attempts:
            logger.info(f"Fix attempt {attempts}: {validation['issues']}")
            fix_prompt = _generate_fix_prompt(code, validation)

            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": fix_prompt}],
                max_tokens=4000,
                temperature=0.3,  # Lower temp for fixes
            )
            code = resp.choices[0].message.content.strip()
            if code.startswith("```"):
                code = code.split("\n", 1)[1].rsplit("```", 1)[0]

            code = _strip_sdk_redefinitions(code)
            validation = _validate_game_code(code)
            attempts += 1

        # Assemble final HTML
        html = _assemble_html(design, config, code)

        return GenResult(
            html=html,
            validation=validation,
            attempts=attempts,
            design=design,
            game_code_lines=code.count("\n") + 1,
        )

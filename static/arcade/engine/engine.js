// ARCADE ENGINE v1 — engine.js
// Core loop, state machine, easings, tweens, springs, pools, utils
// ~550 lines — the backbone everything rides on
"use strict";

// ═══ UTILITIES ═══
const lerp=(a,b,t)=>a+(b-a)*t, clamp=(v,lo,hi)=>v<lo?lo:v>hi?hi:v;
const mapRange=(v,a,b,c,d)=>c+((v-a)/(b-a))*(d-c);
const rng=(a,b)=>a+Math.random()*(b-a), rngI=(a,b)=>Math.floor(rng(a,b+1));
const TAU=Math.PI*2, degRad=d=>d*0.0174533, dist=(x1,y1,x2,y2)=>Math.hypot(x2-x1,y2-y1);

function hexRgb(h){const n=parseInt(h.replace('#',''),16);return[(n>>16)&255,(n>>8)&255,n&255]}
function rgbHex(r,g,b){return'#'+((1<<24)+(r<<16)+(g<<8)+b).toString(16).slice(1)}
function lerpColor(a,b,t){const[r1,g1,b1]=hexRgb(a),[r2,g2,b2]=hexRgb(b);return rgbHex(Math.round(lerp(r1,r2,t)),Math.round(lerp(g1,g2,t)),Math.round(lerp(b1,b2,t)))}
function rgba(hex,a){const[r,g,b]=hexRgb(hex);return`rgba(${r},${g},${b},${a})`}
function hsl(h,s,l,a){return a!==undefined?`hsla(${h},${s}%,${l}%,${a})`:`hsl(${h},${s}%,${l}%)`}

// ═══ EASING (36 functions) ═══
const Ease={
  linear:t=>t,
  inQuad:t=>t*t, outQuad:t=>t*(2-t), ioQuad:t=>t<.5?2*t*t:-1+(4-2*t)*t,
  inCubic:t=>t*t*t, outCubic:t=>(--t)*t*t+1, ioCubic:t=>t<.5?4*t*t*t:(t-1)*(2*t-2)*(2*t-2)+1,
  inQuart:t=>t*t*t*t, outQuart:t=>1-(--t)*t*t*t, ioQuart:t=>t<.5?8*t*t*t*t:1-8*(--t)*t*t*t,
  inQuint:t=>t*t*t*t*t, outQuint:t=>1+(--t)*t*t*t*t, ioQuint:t=>t<.5?16*t*t*t*t*t:1+16*(--t)*t*t*t*t,
  inSine:t=>1-Math.cos(t*Math.PI*.5), outSine:t=>Math.sin(t*Math.PI*.5), ioSine:t=>.5*(1-Math.cos(Math.PI*t)),
  inExpo:t=>t===0?0:Math.pow(2,10*(t-1)), outExpo:t=>t===1?1:1-Math.pow(2,-10*t),
  ioExpo:t=>{if(t===0||t===1)return t;return t<.5?.5*Math.pow(2,20*t-10):1-.5*Math.pow(2,-20*t+10)},
  inCirc:t=>1-Math.sqrt(1-t*t), outCirc:t=>Math.sqrt(1-(--t)*t),
  ioCirc:t=>t<.5?.5*(1-Math.sqrt(1-4*t*t)):.5*(Math.sqrt(1-(2*t-2)*(2*t-2))+1),
  inBack:t=>{const s=1.70158;return t*t*((s+1)*t-s)},
  outBack:t=>{const s=1.70158,t1=t-1;return t1*t1*((s+1)*t1+s)+1},
  ioBack:t=>{const s=2.5949;return t<.5?.5*(4*t*t*((s+1)*2*t-s)):.5*((2*t-2)*(2*t-2)*((s+1)*(2*t-2)+s)+2)},
  inElastic:t=>t===0||t===1?t:-Math.pow(2,10*(t-1))*Math.sin((t-1.1)*5*Math.PI),
  outElastic:t=>t===0||t===1?t:Math.pow(2,-10*t)*Math.sin((t-.1)*5*Math.PI)+1,
  ioElastic:t=>{if(t===0||t===1)return t;t*=2;return t<1?-.5*Math.pow(2,10*(t-1))*Math.sin((t-1.1)*5*Math.PI):.5*Math.pow(2,-10*(t-1))*Math.sin((t-1.1)*5*Math.PI)+1},
  inBounce:t=>1-Ease.outBounce(1-t),
  outBounce:t=>{if(t<1/2.75)return 7.5625*t*t;if(t<2/2.75){t-=1.5/2.75;return 7.5625*t*t+.75}if(t<2.5/2.75){t-=2.25/2.75;return 7.5625*t*t+.9375}t-=2.625/2.75;return 7.5625*t*t+.984375},
  ioBounce:t=>t<.5?.5*Ease.inBounce(t*2):.5*Ease.outBounce(t*2-1)+.5,
  // Extra: spring-like (overshoots then settles)
  springOut:t=>{const s=1-t;return 1-s*s*Math.cos(s*Math.PI*3)},
  punch:t=>{if(t===0||t===1)return t===1?0:0;return Math.sin(t*Math.PI*4)*Math.pow(1-t,2)},
};
const resolveEase=e=>typeof e==='function'?e:Ease[e]||Ease.outCubic;

// ═══ SPRING PHYSICS ═══
class Spring {
  constructor(o={}){
    this.val=o.from||0; this.target=o.to!==undefined?o.to:this.val;
    this.vel=0; this.stiffness=o.stiffness||180; this.damping=o.damping||12;
    this.mass=o.mass||1; this.precision=o.precision||0.001; this.settled=false;
    this.onUpdate=o.onUpdate||null; this.onSettle=o.onSettle||null;
  }
  setTarget(t){this.target=t;this.settled=false}
  update(dt){
    if(this.settled)return this.val;
    const d=this.val-this.target;
    const a=(-this.stiffness*d-this.damping*this.vel)/this.mass;
    this.vel+=a*dt; this.val+=this.vel*dt;
    if(Math.abs(this.vel)<this.precision&&Math.abs(d)<this.precision){
      this.val=this.target;this.vel=0;this.settled=true;
      if(this.onSettle)this.onSettle(this.val);
    }
    if(this.onUpdate)this.onUpdate(this.val,this.vel);
    return this.val;
  }
  impulse(f){this.vel+=f;this.settled=false}
  snap(v){this.val=v;this.target=v;this.vel=0;this.settled=true}
}

class SpringGroup {
  constructor(props,o={}){
    this.springs={};
    for(const[k,v]of Object.entries(props))this.springs[k]=new Spring({from:v,to:v,...o});
  }
  setTargets(t){for(const[k,v]of Object.entries(t))if(this.springs[k])this.springs[k].setTarget(v)}
  update(dt){const r={};for(const[k,s]of Object.entries(this.springs))r[k]=s.update(dt);return r}
  impulse(f){for(const[k,v]of Object.entries(f))if(this.springs[k])this.springs[k].impulse(v)}
  get settled(){return Object.values(this.springs).every(s=>s.settled)}
  get values(){const v={};for(const[k,s]of Object.entries(this.springs))v[k]=s.val;return v}
}

// ═══ TWEEN SYSTEM ═══
class Tween {
  constructor(target){
    this.target=target;this._steps=[];this._cur=0;this._elapsed=0;
    this._running=false;this._finished=false;this._loop=0;this._loopN=0;
    this._yoyo=false;this._dir=1;this._onDone=null;this._onTick=null;
  }
  to(props,ms,ease){this._steps.push({t:'to',props,dur:ms/1000,ease:resolveEase(ease||'outCubic'),sv:null});return this}
  from(props,ms,ease){this._steps.push({t:'from',props,dur:ms/1000,ease:resolveEase(ease||'outCubic'),ev:null});return this}
  delay(ms){this._steps.push({t:'delay',dur:ms/1000});return this}
  call(fn){this._steps.push({t:'call',fn});return this}
  loop(n=-1){this._loop=n;return this}
  yoyo(){this._yoyo=true;this._loop=this._loop||(-1);return this}
  onComplete(fn){this._onDone=fn;return this}
  onUpdate(fn){this._onTick=fn;return this}

  start(){
    this._running=true;this._finished=false;this._cur=0;this._elapsed=0;this._loopN=0;this._dir=1;
    this._initStep(0);TweenMgr.add(this);return this;
  }
  stop(){this._running=false;this._finished=true;TweenMgr.remove(this);return this}

  _initStep(i){
    if(i<0||i>=this._steps.length)return;
    const s=this._steps[i];this._elapsed=0;
    if(s.t==='to'){s.sv={};for(const k of Object.keys(s.props))s.sv[k]=this.target[k]!==undefined?this.target[k]:0}
    else if(s.t==='from'){s.ev={};for(const k of Object.keys(s.props)){s.ev[k]=this.target[k]!==undefined?this.target[k]:0;this.target[k]=s.props[k]}}
  }

  update(dt){
    if(!this._running||this._finished)return false;
    const s=this._steps[this._cur];
    if(!s){this._finish();return false}
    if(s.t==='call'){s.fn(this.target);this._advance();return true}
    this._elapsed+=dt;
    const dur=s.dur||0.001;
    let t=clamp(this._elapsed/dur,0,1);
    if(s.t==='to'){const e=s.ease(t);for(const k of Object.keys(s.props))this.target[k]=lerp(s.sv[k],s.props[k],e)}
    else if(s.t==='from'){const e=s.ease(t);for(const k of Object.keys(s.props))this.target[k]=lerp(s.props[k],s.ev[k],e)}
    if(this._onTick)this._onTick(this.target,t);
    if(t>=1)this._advance();
    return true;
  }

  _advance(){
    this._cur+=this._dir;
    if(this._cur>=this._steps.length||this._cur<0){
      if(this._loop===0){this._finish();return}
      if(this._yoyo){this._dir*=-1;this._cur+=this._dir}else this._cur=0;
      this._loopN++;
      if(this._loop>0&&this._loopN>=this._loop){this._finish();return}
    }
    this._initStep(this._cur);
  }
  _finish(){this._running=false;this._finished=true;TweenMgr.remove(this);if(this._onDone)this._onDone(this.target)}
}

class TweenParallel{
  constructor(tw){this._tw=tw;this._running=false;this._onDone=null}
  onComplete(fn){this._onDone=fn;return this}
  start(){this._running=true;for(const t of this._tw)t.start();TweenMgr.add(this);return this}
  stop(){this._running=false;for(const t of this._tw)t.stop();TweenMgr.remove(this)}
  update(dt){if(!this._running)return false;let any=false;for(const t of this._tw)if(t.update(dt))any=true;if(!any){this._running=false;TweenMgr.remove(this);if(this._onDone)this._onDone()}return any}
}

class TweenSequence{
  constructor(tw){this._tw=tw;this._i=0;this._running=false;this._onDone=null}
  onComplete(fn){this._onDone=fn;return this}
  start(){this._running=true;this._i=0;if(this._tw.length)this._tw[0].start();TweenMgr.add(this);return this}
  stop(){this._running=false;for(const t of this._tw)t.stop();TweenMgr.remove(this)}
  update(dt){
    if(!this._running)return false;
    if(this._i>=this._tw.length){this._running=false;TweenMgr.remove(this);if(this._onDone)this._onDone();return false}
    if(!this._tw[this._i].update(dt)){this._i++;if(this._i<this._tw.length)this._tw[this._i].start()}
    return true;
  }
}

const TweenMgr={
  _list:[],
  add(t){if(!this._list.includes(t))this._list.push(t)},
  remove(t){const i=this._list.indexOf(t);if(i!==-1)this._list.splice(i,1)},
  update(dt){for(let i=this._list.length-1;i>=0;i--)this._list[i].update(dt)},
  killAll(){for(const t of this._list)if(t.stop)t.stop();this._list.length=0},
  killTarget(tgt){this._list=this._list.filter(t=>{if(t.target===tgt){if(t.stop)t.stop();return false}return true})},
  get count(){return this._list.length},
};

// Convenience
const tw=(target)=>new Tween(target);
const twParallel=(...t)=>new TweenParallel(t);
const twSequence=(...t)=>new TweenSequence(t);

// ═══ STATE MACHINE ═══
const GS={IDLE:'idle',BETTING:'betting',PLAYING:'playing',RESOLVING:'resolving',RESULT:'result',ANIMATING:'animating'};

class StateMachine{
  constructor(){this.state=GS.IDLE;this._l={};this.data={}}
  on(e,fn){(this._l[e]||(this._l[e]=[])).push(fn);return this}
  off(e,fn){const a=this._l[e];if(a)this._l[e]=a.filter(f=>f!==fn)}
  emit(e,d){const a=this._l[e];if(a)for(const f of a)f(d)}
  go(to,data){const from=this.state;if(from===to)return;this.state=to;this.data=data||{};this.emit('change',{from,to,data:this.data});this.emit(to,this.data)}
  is(s){return this.state===s}
  isAny(...s){return s.includes(this.state)}
}

// ═══ EVENT BUS ═══
class EventBus{
  constructor(){this._m={}}
  on(e,fn){(this._m[e]||(this._m[e]=[])).push(fn);return()=>this.off(e,fn)}
  once(e,fn){const w=d=>{this.off(e,w);fn(d)};return this.on(e,w)}
  off(e,fn){const a=this._m[e];if(a)this._m[e]=a.filter(f=>f!==fn)}
  emit(e,d){const a=this._m[e];if(a)for(const f of a)f(d)}
  clear(){this._m={}}
}

// ═══ OBJECT POOL ═══
class Pool{
  constructor(factory,reset,n=64){
    this.factory=factory;this.resetFn=reset;this.pool=[];this.active=[];
    for(let i=0;i<n;i++)this.pool.push(factory());
  }
  get(){const o=this.pool.length?this.pool.pop():this.factory();this.resetFn(o);this.active.push(o);return o}
  release(o){const i=this.active.indexOf(o);if(i!==-1){this.active.splice(i,1);this.pool.push(o)}}
  releaseAll(){while(this.active.length)this.pool.push(this.active.pop())}
  forEach(fn){for(let i=this.active.length-1;i>=0;i--)fn(this.active[i],i)}
  get count(){return this.active.length}
}

// ═══ PERF TIER ═══
const Perf={HIGH:'high',MED:'medium',LOW:'low'};
function detectPerf(){
  const c=document.createElement('canvas');
  const gl=c.getContext('webgl')||c.getContext('experimental-webgl');
  const cores=navigator.hardwareConcurrency||2,mem=navigator.deviceMemory||4;
  const mob=/Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
  let s=0;if(gl)s+=3;if(cores>=4)s+=2;else if(cores>=2)s+=1;if(mem>=4)s+=2;else if(mem>=2)s+=1;if(!mob)s+=1;
  return s>=7?Perf.HIGH:s>=4?Perf.MED:Perf.LOW;
}

// ═══ GAME LOOP ═══
class GameLoop{
  constructor(o={}){
    this.fixedDt=1000/(o.physicsHz||60);this.maxDelta=o.maxDelta||100;
    this.acc=0;this.time=0;this.frame=0;this.raf=null;this.running=false;this.paused=false;
    this.timeScale=1;
    this.updateFn=o.update||(()=>{});this.renderFn=o.render||(()=>{});this.fixedFn=o.fixedUpdate||(()=>{});
    this.fps=60;this._fpsN=0;this._fpsT=0;
    this.perf=detectPerf();this._degN=0;this._upN=0;
    this._vis=()=>{document.hidden?this.pause():this.resume()};
    this._tick=this._tick.bind(this);
    document.addEventListener('visibilitychange',this._vis);
  }
  start(){if(this.running)return;this.running=true;this.paused=false;this._last=performance.now();this.raf=requestAnimationFrame(this._tick)}
  stop(){this.running=false;if(this.raf){cancelAnimationFrame(this.raf);this.raf=null}}
  pause(){this.paused=true}
  resume(){if(this.paused){this.paused=false;this._last=performance.now();this.acc=0}}

  _tick(now){
    if(!this.running)return;
    this.raf=requestAnimationFrame(this._tick);
    if(this.paused)return;
    let raw=now-this._last;this._last=now;
    if(raw>this.maxDelta)raw=this.maxDelta;
    const delta=raw*this.timeScale;this.time+=delta;this.frame++;
    this._fpsN++;this._fpsT+=raw;
    if(this._fpsT>=500){this.fps=(this._fpsN/this._fpsT)*1000;this._fpsN=0;this._fpsT=0;this._perfCheck()}
    this.acc+=delta;let steps=0;
    while(this.acc>=this.fixedDt&&steps<4){this.fixedFn(this.fixedDt/1000,this.time/1000);this.acc-=this.fixedDt;steps++}
    const dt=delta/1000,t=this.time/1000;
    this.updateFn(dt,t);this.renderFn(dt,t,this.acc/this.fixedDt);
  }

  _perfCheck(){
    if(this.fps<28&&this.perf!==Perf.LOW){if(++this._degN>=3){this.perf=this.perf===Perf.HIGH?Perf.MED:Perf.LOW;this._degN=0;this._upN=0}}
    else if(this.fps>55&&this.perf!==Perf.HIGH){if(++this._upN>=10){this.perf=this.perf===Perf.LOW?Perf.MED:Perf.HIGH;this._upN=0;this._degN=0}}
    else this._degN=Math.max(0,this._degN-1);
  }
  destroy(){this.stop();document.removeEventListener('visibilitychange',this._vis)}
}

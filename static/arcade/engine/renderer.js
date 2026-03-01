// ARCADE ENGINE v1 — renderer.js
// Canvas renderer, particle system, camera, screen effects
"use strict";

// ═══ RENDERER ═══
class Renderer{
  constructor(container,o={}){
    this.el=typeof container==='string'?document.getElementById(container):container;
    this.dpr=Math.min(window.devicePixelRatio||1,o.maxDpr||2);
    this.W=0;this.H=0;
    this.bg=this._mkCanvas('arc-bg');this.fg=this._mkCanvas('arc-fg');
    this.bgx=this.bg.getContext('2d');this.fgx=this.fg.getContext('2d');
    this._fx=document.createElement('canvas');this._fxCtx=this._fx.getContext('2d');
    this._sprites={};this._bgDirty=true;this._bgFn=null;
    this._ro=new ResizeObserver(()=>this._resize());this._ro.observe(this.el);
    this._resize();
  }
  _mkCanvas(id){const c=document.createElement('canvas');c.id=id;c.style.cssText='position:absolute;inset:0;width:100%;height:100%';this.el.appendChild(c);return c}
  _resize(){
    const r=this.el.getBoundingClientRect();this.W=r.width;this.H=r.height;
    const pw=Math.round(r.width*this.dpr),ph=Math.round(r.height*this.dpr);
    for(const c of[this.bg,this.fg,this._fx]){c.width=pw;c.height=ph}
    for(const x of[this.bgx,this.fgx,this._fxCtx])x.setTransform(this.dpr,0,0,this.dpr,0,0);
    this._bgDirty=true;
  }
  setBgDraw(fn){this._bgFn=fn;this._bgDirty=true}
  invalidateBg(){this._bgDirty=true}
  drawBg(){if(!this._bgDirty||!this._bgFn)return;this.bgx.clearRect(0,0,this.W,this.H);this._bgFn(this.bgx,this.W,this.H);this._bgDirty=false}
  clear(){this.fgx.clearRect(0,0,this.W,this.H)}
  get ctx(){return this.fgx}

  // Sprite cache: emoji → offscreen canvas
  sprite(emoji,sz=32){
    const k=emoji+'_'+sz;if(this._sprites[k])return this._sprites[k];
    const s=sz*this.dpr,c=document.createElement('canvas');c.width=s;c.height=s;
    const x=c.getContext('2d');x.font=`${s*.8}px serif`;x.textAlign='center';x.textBaseline='middle';x.fillText(emoji,s/2,s/2);
    this._sprites[k]=c;return c;
  }
  drawSprite(emoji,x,y,sz=32,rot=0,alpha=1,sx=1,sy=1){
    const sp=this.sprite(emoji,sz),c=this.fgx;
    c.save();c.globalAlpha=alpha;c.translate(x,y);
    if(rot)c.rotate(rot);if(sx!==1||sy!==1)c.scale(sx,sy);
    c.drawImage(sp,-sz/2,-sz/2,sz,sz);c.restore();
  }

  // Drawing helpers
  circle(x,y,r,fill,stroke,lw){const c=this.fgx;c.beginPath();c.arc(x,y,r,0,TAU);if(fill){c.fillStyle=fill;c.fill()}if(stroke){c.strokeStyle=stroke;c.lineWidth=lw||1;c.stroke()}}
  rect(x,y,w,h,fill,stroke,lw,rad){const c=this.fgx;c.beginPath();rad?c.roundRect(x,y,w,h,rad):c.rect(x,y,w,h);if(fill){c.fillStyle=fill;c.fill()}if(stroke){c.strokeStyle=stroke;c.lineWidth=lw||1;c.stroke()}}
  line(x1,y1,x2,y2,color,w=1){const c=this.fgx;c.beginPath();c.moveTo(x1,y1);c.lineTo(x2,y2);c.strokeStyle=color;c.lineWidth=w;c.stroke()}

  text(txt,x,y,o={}){
    const c=this.fgx;c.save();
    c.font=`${o.weight||'700'} ${o.size||14}px ${o.font||'Inter,sans-serif'}`;
    c.textAlign=o.align||'center';c.textBaseline=o.base||'middle';c.globalAlpha=o.alpha!==undefined?o.alpha:1;
    if(o.glow){c.shadowColor=o.glow;c.shadowBlur=o.glowR||10}
    if(o.stroke){c.strokeStyle=o.stroke;c.lineWidth=o.strokeW||3;c.strokeText(txt,x,y)}
    c.fillStyle=o.color||'#fff';c.fillText(txt,x,y);c.restore();
  }

  gradient(x,y,w,h,colors,angle=0,rad=0){
    const c=this.fgx,a=angle*Math.PI/180,cos=Math.cos(a),sin=Math.sin(a);
    const cx=x+w/2,cy=y+h/2,l=Math.max(w,h);
    const g=c.createLinearGradient(cx-cos*l/2,cy-sin*l/2,cx+cos*l/2,cy+sin*l/2);
    for(let i=0;i<colors.length;i++)g.addColorStop(i/(colors.length-1),colors[i]);
    c.beginPath();rad?c.roundRect(x,y,w,h,rad):c.rect(x,y,w,h);c.fillStyle=g;c.fill();
  }

  radialGlow(x,y,r,color,alpha=.5){
    const c=this.fgx,g=c.createRadialGradient(x,y,0,x,y,r);
    g.addColorStop(0,rgba(color,alpha));g.addColorStop(1,rgba(color,0));
    c.fillStyle=g;c.fillRect(x-r,y-r,r*2,r*2);
  }

  // Post-processing
  bloom(intensity=.3){
    const c=this.fgx,e=this._fxCtx;
    e.clearRect(0,0,this.W,this.H);
    e.filter=`blur(${8*intensity}px) brightness(${1+intensity})`;
    e.drawImage(this.fg,0,0,this.W,this.H);e.filter='none';
    c.save();c.globalAlpha=intensity;c.globalCompositeOperation='screen';
    c.drawImage(this._fx,0,0,this.W,this.H);c.restore();
  }

  vignette(intensity=.3){
    const c=this.fgx,cx=this.W/2,cy=this.H/2,r=Math.max(this.W,this.H)*.7;
    const g=c.createRadialGradient(cx,cy,r*.3,cx,cy,r);
    g.addColorStop(0,'rgba(0,0,0,0)');g.addColorStop(1,`rgba(0,0,0,${intensity})`);
    c.fillStyle=g;c.fillRect(0,0,this.W,this.H);
  }

  flash(color,alpha){
    if(alpha<=0)return;const c=this.fgx;c.save();c.globalAlpha=alpha;c.fillStyle=color;c.fillRect(0,0,this.W,this.H);c.restore();
  }

  borderGlow(color,alpha,w=4){
    if(alpha<=0)return;const c=this.fgx;c.save();c.globalAlpha=alpha;
    c.strokeStyle=color;c.lineWidth=w;c.shadowColor=color;c.shadowBlur=20;
    c.strokeRect(1,1,this.W-2,this.H-2);c.restore();
  }

  destroy(){this._ro.disconnect();this.bg.remove();this.fg.remove();this._sprites={}}
}

// ═══ PARTICLE SYSTEM (Structure of Arrays — cache-friendly, GC-free) ═══
class Particles{
  constructor(renderer,o={}){
    this.R=renderer;const N=o.max||500;this.max=N;
    this.gravity=o.gravity||0;this.wind=o.wind||0;this.turb=o.turb||0;
    // SoA
    this.x=new Float32Array(N);this.y=new Float32Array(N);
    this.vx=new Float32Array(N);this.vy=new Float32Array(N);
    this.life=new Float32Array(N);this.maxLife=new Float32Array(N);
    this.sz=new Float32Array(N);this.szEnd=new Float32Array(N);
    this.rot=new Float32Array(N);this.rotV=new Float32Array(N);
    this.a=new Float32Array(N);this.aEnd=new Float32Array(N);
    this.ci=new Uint8Array(N);this.type=new Uint8Array(N);// 0=circle,1=square,2=emoji
    this.emojis=[];this._emojiSp=[];this.n=0;
  }
  setEmojis(list){this.emojis=list;this._emojiSp=list.map(e=>this.R.sprite(e,24))}

  emit(o){
    const count=o.count||1;
    for(let i=0;i<count;i++){
      if(this.n>=this.max)return;
      const idx=this.n++;
      // Position + spread
      let px=o.x||0,py=o.y||0;
      if(o.spread){
        if(o.spreadShape==='circle'){const a=Math.random()*TAU,r=Math.random()*o.spread;px+=Math.cos(a)*r;py+=Math.sin(a)*r}
        else if(o.spreadShape==='ring'){const a=Math.random()*TAU;px+=Math.cos(a)*o.spread;py+=Math.sin(a)*o.spread}
        else{px+=(Math.random()-.5)*o.spread;py+=(Math.random()-.5)*o.spread}
      }
      this.x[idx]=px;this.y[idx]=py;
      // Velocity
      if(o.angle!==undefined){
        const a=o.angle+(o.angleSpread?(Math.random()-.5)*o.angleSpread:0);
        const s=(o.speed||100)+(o.speedVar?(Math.random()-.5)*o.speedVar:0);
        this.vx[idx]=Math.cos(a)*s;this.vy[idx]=Math.sin(a)*s;
      }else{
        const s=(o.speed||100)+(o.speedVar?(Math.random()-.5)*o.speedVar:0);
        const a=Math.random()*TAU;this.vx[idx]=Math.cos(a)*s;this.vy[idx]=Math.sin(a)*s;
      }
      // Life
      const lBase=o.life||1;this.maxLife[idx]=lBase+(o.lifeVar?(Math.random()-.5)*o.lifeVar:0);this.life[idx]=this.maxLife[idx];
      // Size
      this.sz[idx]=o.size||4;this.szEnd[idx]=o.sizeEnd!==undefined?o.sizeEnd:0;
      // Rotation
      this.rot[idx]=o.rotation||(Math.random()*TAU);this.rotV[idx]=o.rotSpeed||(Math.random()-0.5)*4;
      // Alpha
      this.a[idx]=o.alpha!==undefined?o.alpha:1;this.aEnd[idx]=o.alphaEnd!==undefined?o.alphaEnd:0;
      // Color & type
      this.ci[idx]=o.colorIdx!==undefined?o.colorIdx:Math.floor(Math.random()*Math.max(1,this.emojis.length));
      this.type[idx]=o.emoji?2:o.square?1:0;
    }
  }

  update(dt){
    for(let i=this.n-1;i>=0;i--){
      this.life[i]-=dt;
      if(this.life[i]<=0){// Swap-remove
        this.n--;if(i<this.n){
          this.x[i]=this.x[this.n];this.y[i]=this.y[this.n];this.vx[i]=this.vx[this.n];this.vy[i]=this.vy[this.n];
          this.life[i]=this.life[this.n];this.maxLife[i]=this.maxLife[this.n];this.sz[i]=this.sz[this.n];this.szEnd[i]=this.szEnd[this.n];
          this.rot[i]=this.rot[this.n];this.rotV[i]=this.rotV[this.n];this.a[i]=this.a[this.n];this.aEnd[i]=this.aEnd[this.n];
          this.ci[i]=this.ci[this.n];this.type[i]=this.type[this.n];
        }continue;
      }
      this.vx[i]+=this.wind*dt;this.vy[i]+=this.gravity*dt;
      if(this.turb>0){this.vx[i]+=(Math.random()-.5)*this.turb*dt;this.vy[i]+=(Math.random()-.5)*this.turb*dt}
      this.x[i]+=this.vx[i]*dt;this.y[i]+=this.vy[i]*dt;this.rot[i]+=this.rotV[i]*dt;
    }
  }

  render(colors=['#fff']){
    const c=this.R.fgx;
    for(let i=0;i<this.n;i++){
      const t=1-(this.life[i]/this.maxLife[i]);
      const sz=lerp(this.sz[i],this.szEnd[i],t);
      const al=lerp(this.a[i],this.aEnd[i],t);
      if(al<=.01||sz<=.1)continue;
      c.save();c.globalAlpha=al;c.translate(this.x[i],this.y[i]);c.rotate(this.rot[i]);
      if(this.type[i]===2&&this._emojiSp.length>0){
        const sp=this._emojiSp[this.ci[i]%this._emojiSp.length];c.drawImage(sp,-sz/2,-sz/2,sz,sz);
      }else if(this.type[i]===1){c.fillStyle=colors[this.ci[i]%colors.length];c.fillRect(-sz/2,-sz/2,sz,sz)}
      else{c.fillStyle=colors[this.ci[i]%colors.length];c.beginPath();c.arc(0,0,sz/2,0,TAU);c.fill()}
      c.restore();
    }
  }
  clear(){this.n=0}
}

// ═══ PARTICLE PRESETS ═══
const PFX={
  burst(ps,x,y,o={}){ps.emit({x,y,count:o.count||30,speed:o.speed||200,speedVar:o.speedVar||100,life:o.life||.8,lifeVar:.4,size:o.size||8,sizeEnd:0,alpha:1,alphaEnd:0,emoji:o.emoji!==false})},
  confetti(ps,x,y,o={}){ps.emit({x,y,count:o.count||50,spread:o.spread||100,angle:-Math.PI/2,angleSpread:Math.PI*.8,speed:300,speedVar:150,life:2,lifeVar:.5,size:10,sizeEnd:4,alpha:1,alphaEnd:.2,rotSpeed:8,square:true})},
  fountain(ps,x,y,o={}){ps.emit({x,y,count:o.count||20,angle:-Math.PI/2,angleSpread:.6,speed:250,speedVar:80,life:1.2,lifeVar:.3,size:6,sizeEnd:2,alpha:1,alphaEnd:0,emoji:o.emoji!==false})},
  sparkle(ps,x,y,o={}){if(Math.random()>(o.rate||.3))return;ps.emit({x,y,count:1,spread:o.spread||50,spreadShape:'circle',speed:20,life:.6,lifeVar:.3,size:o.size||4,sizeEnd:0,alpha:.8,alphaEnd:0})},
  trail(ps,x,y,o={}){ps.emit({x,y,count:o.count||2,spread:3,speed:15,speedVar:10,life:o.life||.4,size:o.size||5,sizeEnd:0,alpha:.7,alphaEnd:0,emoji:o.emoji!==false})},
  ring(ps,x,y,o={}){const n=o.count||24;for(let i=0;i<n;i++)ps.emit({x,y,count:1,angle:(TAU/n)*i,angleSpread:0,speed:o.speed||150,life:o.life||.6,size:4,sizeEnd:0,alpha:.8,alphaEnd:0})},
  impact(ps,x,y,angle,o={}){ps.emit({x,y,count:o.count||12,angle:angle+Math.PI,angleSpread:Math.PI*.5,speed:o.speed||120,speedVar:60,life:.5,lifeVar:.2,size:5,sizeEnd:1,alpha:1,alphaEnd:0})},
  coinShower(ps,x,y,o={}){ps.emit({x,y:y-50,count:o.count||40,spread:200,angle:Math.PI/2,angleSpread:1,speed:80,speedVar:40,life:2.5,lifeVar:.5,size:16,sizeEnd:12,alpha:1,alphaEnd:.3,rotSpeed:5,emoji:true})},
  fire(ps,x,y,o={}){ps.emit({x,y,count:o.count||5,spread:10,angle:-Math.PI/2,angleSpread:.4,speed:60,speedVar:30,life:.6,lifeVar:.2,size:8,sizeEnd:2,alpha:.9,alphaEnd:0})},
};

// ═══ CAMERA & SCREEN EFFECTS ═══
class Camera{
  constructor(){
    this.x=0;this.y=0;this.zoom=1;this._zoomSpring=new Spring({from:1,to:1,stiffness:200,damping:15});
    // Shake
    this._si=0;this._sd=0;this._sf=0;this._st=0;this.shakeX=0;this.shakeY=0;
    // Screen effects
    this.flashColor='#fff';this.flashAlpha=0;this.flashDecay=0;
    this.glowColor='#fff';this.glowAlpha=0;this.glowDecay=0;
    this.vignetteVal=0;this.vignetteTarget=0;
    // Pulse
    this._pa=0;this._pf=0;this._pd=0;
    // Follow
    this._ft=null;this._fs=.1;
  }
  shake(intensity=10,dur=.3,freq=30){this._si=intensity;this._sd=intensity/dur;this._sf=freq;this._st=0}
  heavyShake(){this.shake(15,.5,25)}
  lightShake(){this.shake(4,.15,40)}
  zoomTo(z){this._zoomSpring.setTarget(z)}
  zoomPulse(amt=.15,dur=.3){this._pa=amt;this._pf=Math.PI/dur;this._pd=1/dur}
  screenFlash(color='#fff',alpha=.6,decay=3){this.flashColor=color;this.flashAlpha=alpha;this.flashDecay=decay}
  borderPulse(color='#fff',alpha=.5,decay=2){this.glowColor=color;this.glowAlpha=alpha;this.glowDecay=decay}
  setVignette(v){this.vignetteTarget=v}
  follow(target,smooth=.1){this._ft=target;this._fs=smooth}

  update(dt){
    // Shake
    if(this._si>0){
      this._st+=dt;this._si-=this._sd*dt;
      if(this._si<0)this._si=0;
      this.shakeX=Math.sin(this._st*this._sf*TAU)*(Math.random()*.4+.6)*this._si;
      this.shakeY=Math.cos(this._st*this._sf*TAU*1.1)*(Math.random()*.4+.6)*this._si;
    }else{this.shakeX=0;this.shakeY=0}
    // Zoom
    this.zoom=this._zoomSpring.update(dt);
    // Pulse
    if(this._pa>0){this._pa-=this._pd*dt*this._pa;if(this._pa<.001)this._pa=0}
    // Flash
    if(this.flashAlpha>0){this.flashAlpha-=this.flashDecay*dt;if(this.flashAlpha<0)this.flashAlpha=0}
    // Border glow
    if(this.glowAlpha>0){this.glowAlpha-=this.glowDecay*dt;if(this.glowAlpha<0)this.glowAlpha=0}
    // Vignette
    this.vignetteVal=lerp(this.vignetteVal,this.vignetteTarget,1-Math.exp(-4*dt));
    // Follow
    if(this._ft){this.x=lerp(this.x,this._ft.x||0,this._fs);this.y=lerp(this.y,this._ft.y||0,this._fs)}
  }

  applyTransform(ctx,W,H){
    const cx=W/2,cy=H/2;
    const z=this.zoom+(this._pa>0?Math.sin(performance.now()*.01*this._pf)*this._pa:0);
    ctx.translate(cx+this.shakeX,cy+this.shakeY);
    ctx.scale(z,z);
    ctx.translate(-cx-this.x,-cy-this.y);
  }

  renderEffects(R){
    if(this.flashAlpha>0)R.flash(this.flashColor,this.flashAlpha);
    if(this.glowAlpha>0)R.borderGlow(this.glowColor,this.glowAlpha);
    if(this.vignetteVal>.01)R.vignette(this.vignetteVal);
  }
}

// ARCADE ENGINE v1 — input.js
// Unified touch/mouse, gestures, haptics
"use strict";

class Input{
  constructor(el){
    this.el=typeof el==='string'?document.getElementById(el):el;
    // State
    this.x=0;this.y=0;this.down=false;this.pressed=false;this.released=false;
    this.dragX=0;this.dragY=0;this.isDragging=false;
    this._startX=0;this._startY=0;this._startTime=0;
    this._prevX=0;this._prevY=0;
    this.velX=0;this.velY=0;
    // Listeners
    this._handlers=[];
    this._cb={click:null,dragStart:null,drag:null,dragEnd:null,swipe:null,hover:null,longPress:null};
    // Config
    this.dragThreshold=8;
    this.longPressMs=500;
    this._lpTimer=null;
    this._bound={};
    this._init();
  }

  _init(){
    const el=this.el;
    // Prevent defaults that mess with games
    el.style.touchAction='none';
    el.style.userSelect='none';
    el.style.webkitUserSelect='none';

    const b=this._bound;
    b.pointerDown=e=>this._onDown(e);
    b.pointerMove=e=>this._onMove(e);
    b.pointerUp=e=>this._onUp(e);
    b.pointerLeave=e=>this._onLeave(e);

    el.addEventListener('pointerdown',b.pointerDown);
    el.addEventListener('pointermove',b.pointerMove);
    el.addEventListener('pointerup',b.pointerUp);
    el.addEventListener('pointerleave',b.pointerLeave);
    el.addEventListener('pointercancel',b.pointerUp);

    // Prevent context menu on long press
    el.addEventListener('contextmenu',e=>e.preventDefault());
  }

  _pos(e){
    const r=this.el.getBoundingClientRect();
    return{x:e.clientX-r.left,y:e.clientY-r.top};
  }

  _onDown(e){
    e.preventDefault();
    const p=this._pos(e);
    this.x=p.x;this.y=p.y;
    this._startX=p.x;this._startY=p.y;
    this._startTime=performance.now();
    this._prevX=p.x;this._prevY=p.y;
    this.down=true;this.pressed=true;this.isDragging=false;
    this.velX=0;this.velY=0;

    // Long press timer
    clearTimeout(this._lpTimer);
    this._lpTimer=setTimeout(()=>{
      if(this.down&&!this.isDragging){
        if(this._cb.longPress)this._cb.longPress({x:this.x,y:this.y});
        this.haptic([30]);
      }
    },this.longPressMs);

    this.el.setPointerCapture(e.pointerId);
  }

  _onMove(e){
    const p=this._pos(e);
    this.velX=p.x-this._prevX;
    this.velY=p.y-this._prevY;
    this._prevX=p.x;this._prevY=p.y;
    this.x=p.x;this.y=p.y;

    if(!this.down){
      // Hover
      if(this._cb.hover)this._cb.hover({x:p.x,y:p.y});
      return;
    }

    this.dragX=p.x-this._startX;
    this.dragY=p.y-this._startY;

    if(!this.isDragging&&Math.hypot(this.dragX,this.dragY)>this.dragThreshold){
      this.isDragging=true;
      clearTimeout(this._lpTimer);
      if(this._cb.dragStart)this._cb.dragStart({x:this._startX,y:this._startY});
    }

    if(this.isDragging&&this._cb.drag){
      this._cb.drag({x:p.x,y:p.y,dx:this.dragX,dy:this.dragY,velX:this.velX,velY:this.velY});
    }
  }

  _onUp(e){
    if(!this.down)return;
    const p=this._pos(e);
    this.x=p.x;this.y=p.y;
    this.down=false;this.released=true;
    clearTimeout(this._lpTimer);

    if(this.isDragging){
      if(this._cb.dragEnd)this._cb.dragEnd({x:p.x,y:p.y,dx:this.dragX,dy:this.dragY,velX:this.velX,velY:this.velY});
      // Check swipe
      const elapsed=(performance.now()-this._startTime)/1000;
      const dist=Math.hypot(this.dragX,this.dragY);
      if(dist>50&&elapsed<.5){
        const angle=Math.atan2(this.dragY,this.dragX);
        let dir='right';
        if(angle>Math.PI*.25&&angle<Math.PI*.75)dir='down';
        else if(angle<-Math.PI*.25&&angle>-Math.PI*.75)dir='up';
        else if(Math.abs(angle)>Math.PI*.75)dir='left';
        if(this._cb.swipe)this._cb.swipe({dir,dist,speed:dist/elapsed,angle});
      }
      this.isDragging=false;
    }else{
      // Click/tap
      if(this._cb.click)this._cb.click({x:p.x,y:p.y});
    }
  }

  _onLeave(e){
    if(this.down){
      this._onUp(e);
    }
  }

  // Register callbacks
  onClick(fn){this._cb.click=fn;return this}
  onDragStart(fn){this._cb.dragStart=fn;return this}
  onDrag(fn){this._cb.drag=fn;return this}
  onDragEnd(fn){this._cb.dragEnd=fn;return this}
  onSwipe(fn){this._cb.swipe=fn;return this}
  onHover(fn){this._cb.hover=fn;return this}
  onLongPress(fn){this._cb.longPress=fn;return this}

  // Hit testing helpers
  hitRect(x,y,w,h){return this.x>=x&&this.x<=x+w&&this.y>=y&&this.y<=y+h}
  hitCircle(cx,cy,r){return dist(this.x,this.y,cx,cy)<=r}

  // Haptic feedback
  haptic(pattern=[10]){
    if(navigator.vibrate)try{navigator.vibrate(pattern)}catch(e){}
  }
  hapticLight(){this.haptic([10])}
  hapticMedium(){this.haptic([15,30,15])}
  hapticHeavy(){this.haptic([100])}
  hapticSuccess(){this.haptic([10,20,10,20,30])}

  // Reset per-frame flags
  resetFrame(){this.pressed=false;this.released=false}

  destroy(){
    const b=this._bound;
    this.el.removeEventListener('pointerdown',b.pointerDown);
    this.el.removeEventListener('pointermove',b.pointerMove);
    this.el.removeEventListener('pointerup',b.pointerUp);
    this.el.removeEventListener('pointerleave',b.pointerLeave);
  }
}

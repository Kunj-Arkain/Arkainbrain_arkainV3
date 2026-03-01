// ARCADE ENGINE v1 — audio.js
// Procedural audio synthesis, musical scales, themed sound palettes
"use strict";

class AudioEngine{
  constructor(){this.ctx=null;this.master=null;this.ch={};this.muted=false;this._ok=false;this._theme='default'}

  async init(){
    if(this._ok)return;
    try{
      this.ctx=new(window.AudioContext||window.webkitAudioContext)();
      this.master=this.ctx.createGain();this.master.gain.value=.7;this.master.connect(this.ctx.destination);
      // Channels
      for(const c of['sfx','music','ui','amb']){
        const g=this.ctx.createGain();
        g.gain.value=c==='music'?.3:c==='amb'?.2:.6;
        g.connect(this.master);this.ch[c]=g;
      }
      // Compressor on SFX
      this.comp=this.ctx.createDynamicsCompressor();
      this.comp.threshold.value=-20;this.comp.ratio.value=4;this.comp.connect(this.ch.sfx);
      this._ok=true;
    }catch(e){console.warn('Audio unavailable',e)}
  }

  async ensure(){
    if(!this._ok)await this.init();
    if(this.ctx&&this.ctx.state==='suspended')try{await this.ctx.resume()}catch(e){}
  }

  setVol(ch,v){if(this.ch[ch])this.ch[ch].gain.setTargetAtTime(v,this.ctx.currentTime,.05)}
  mute(){this.muted=true;if(this.master)this.master.gain.value=0}
  unmute(){this.muted=false;if(this.master)this.master.gain.value=.7}

  // ── Core tone synthesis ──
  tone(o={}){
    if(!this._ok||this.muted)return null;
    const c=this.ctx,now=c.currentTime,start=now+(o.delay||0);
    const freq=o.freq||440,type=o.type||'sine';
    const atk=o.attack||.01,dec=o.decay||.1,sus=o.sustain||0,rel=o.release||.1;
    const vol=o.volume||.3,pan=o.pan||0,ch=o.channel||'sfx';

    const osc=c.createOscillator();osc.type=type;osc.frequency.value=freq;
    if(o.freqEnd){osc.frequency.setValueAtTime(freq,start);osc.frequency.exponentialRampToValueAtTime(Math.max(o.freqEnd,20),start+atk+dec+sus)}

    const env=c.createGain();
    env.gain.setValueAtTime(0,start);
    env.gain.linearRampToValueAtTime(vol,start+atk);
    env.gain.linearRampToValueAtTime(vol*.6,start+atk+dec);
    if(sus>0)env.gain.setValueAtTime(vol*.6,start+atk+dec+sus);
    env.gain.linearRampToValueAtTime(0,start+atk+dec+sus+rel);

    const pn=c.createStereoPanner();pn.pan.value=clamp(pan,-1,1);

    let chain=osc;chain=this._connect(chain,env);
    if(o.filter){
      const f=c.createBiquadFilter();f.type=o.filter.type||'lowpass';f.frequency.value=o.filter.freq||2000;f.Q.value=o.filter.Q||1;
      if(o.filter.freqEnd){f.frequency.setValueAtTime(o.filter.freq||2000,start);f.frequency.exponentialRampToValueAtTime(o.filter.freqEnd,start+atk+dec+sus+rel)}
      chain=this._connect(chain,f);
    }
    chain=this._connect(chain,pn);
    pn.connect(ch==='sfx'?this.comp:(this.ch[ch]||this.master));

    const dur=atk+dec+sus+rel+.1;osc.start(start);osc.stop(start+dur);
    return{osc,env,dur};
  }
  _connect(a,b){a.connect(b);return b}

  // ── Noise ──
  noise(o={}){
    if(!this._ok||this.muted)return;
    const c=this.ctx,now=c.currentTime,dur=o.duration||.2,vol=o.volume||.1;
    const sz=c.sampleRate*dur,buf=c.createBuffer(1,sz,c.sampleRate),d=buf.getChannelData(0);
    for(let i=0;i<sz;i++)d[i]=Math.random()*2-1;
    const src=c.createBufferSource();src.buffer=buf;
    const env=c.createGain();env.gain.setValueAtTime(vol,now);env.gain.exponentialRampToValueAtTime(.001,now+dur);
    const f=c.createBiquadFilter();f.type=o.filterType||'bandpass';f.frequency.value=o.filterFreq||1000;f.Q.value=o.filterQ||.5;
    src.connect(f);f.connect(env);env.connect(this.ch[o.channel||'sfx']||this.comp);
    src.start(now);src.stop(now+dur);
  }

  // ── Musical ──
  noteFreq(midi){return 440*Math.pow(2,(midi-69)/12)}
  playNote(degree,oct=4,o={}){
    const scales={major:[0,2,4,5,7,9,11],minor:[0,2,3,5,7,8,10],penta:[0,2,4,7,9],blues:[0,3,5,6,7,10],arabic:[0,1,4,5,7,8,11],japanese:[0,1,5,7,8],chromatic:[0,1,2,3,4,5,6,7,8,9,10,11]};
    const sc=scales[o.scale||'penta']||scales.penta;
    const root=o.root||0;
    const idx=((degree%sc.length)+sc.length)%sc.length;
    const octShift=Math.floor(degree/sc.length);
    const midi=60+root+(oct-4)*12+sc[idx]+octShift*12;
    return this.tone({...o,freq:this.noteFreq(midi)});
  }

  // ── Preset sounds ──
  click(){this.tone({freq:800,decay:.05,release:.02,type:'square',volume:.15,channel:'ui'})}
  tick(p=0){this.tone({freq:600+p*100,decay:.03,release:.02,type:'triangle',volume:.12})}
  win(intensity=1){
    const v=.2*intensity;
    for(let i=0;i<4;i++)this.playNote(i*2,5,{attack:.01,decay:.15,release:.2,type:'triangle',volume:v,scale:'major',delay:i*.09});
  }
  lose(){this.tone({freq:200,freqEnd:80,decay:.3,release:.2,type:'sawtooth',volume:.15,filter:{type:'lowpass',freq:1000,freqEnd:200}})}
  bigWin(){for(let i=0;i<5;i++)this.playNote(i*2,5,{attack:.01,decay:.2,sustain:.05,release:.3,type:'triangle',volume:.25,scale:'major',delay:i*.1});this.noise({duration:.5,volume:.05,filterType:'highpass',filterFreq:6000})}
  crash(){this.noise({duration:.4,volume:.2,filterType:'lowpass',filterFreq:500,filterQ:2});this.tone({freq:80,freqEnd:30,attack:.01,decay:.3,release:.2,type:'sawtooth',volume:.2})}
  bounce(p=0){this.tone({freq:300+p*50,freqEnd:(300+p*50)*.7,decay:.06,release:.04,type:'triangle',volume:.1})}
  reveal(){this.tone({freq:1200,decay:.08,release:.06,type:'sine',volume:.15});this.tone({freq:1600,decay:.08,release:.06,type:'sine',volume:.1,delay:.05})}
  coinDrop(n=5){for(let i=0;i<n;i++)this.tone({freq:2000+Math.random()*1000,decay:.05+Math.random()*.05,release:.03,type:'sine',volume:.08,delay:i*.05+Math.random()*.02,pan:Math.random()*2-1})}
  cardFlip(){this.noise({duration:.08,volume:.1,filterType:'highpass',filterFreq:3000})}
  shatter(){this.noise({duration:.3,volume:.2,filterType:'highpass',filterFreq:2000,filterQ:3});this.tone({freq:400,freqEnd:100,decay:.15,type:'sawtooth',volume:.12})}
  step(){this.noise({duration:.05,volume:.08,filterType:'bandpass',filterFreq:800,filterQ:2})}
  scratch(i=.5){this.noise({duration:.04+i*.03,volume:.06*i,filterType:'bandpass',filterFreq:2000+i*2000})}
  wheelTick(p=0){this.tone({freq:500+p*200,decay:.02,release:.01,type:'square',volume:.08})}

  // Rising tension (returns controller)
  tension(base=100){
    if(!this._ok||this.muted)return{setIntensity:()=>{},stop:()=>{}};
    const c=this.ctx,osc=c.createOscillator(),g=c.createGain(),f=c.createBiquadFilter();
    osc.type='sawtooth';osc.frequency.value=base;g.gain.value=0;
    f.type='lowpass';f.frequency.value=400;f.Q.value=5;
    osc.connect(f);f.connect(g);g.connect(this.ch.amb);osc.start();
    return{
      setIntensity:t=>{const n=c.currentTime;g.gain.setTargetAtTime(t*.15,n,.1);osc.frequency.setTargetAtTime(base+t*200,n,.1);f.frequency.setTargetAtTime(400+t*2000,n,.1)},
      stop:()=>{const n=c.currentTime;g.gain.setTargetAtTime(0,n,.1);osc.stop(n+.3)}
    };
  }

  // Theme palette
  setTheme(t){this._theme=t}
  get themeScale(){return{egyptian:'arabic',space:'penta',ocean:'japanese',fire:'blues',jungle:'penta',cyberpunk:'chromatic',luxury:'major',arctic:'minor'}[this._theme]||'penta'}
  get themeType(){return{egyptian:'triangle',space:'sine',ocean:'sine',fire:'sawtooth',jungle:'triangle',cyberpunk:'square',luxury:'sine',arctic:'sine'}[this._theme]||'triangle'}
  themeBounce(p=0){this.playNote(p,5,{scale:this.themeScale,type:this.themeType,decay:.08,release:.05,volume:.1})}
  themeWin(i=1){const v=.2*i;for(let j=0;j<4;j++)this.playNote(j*2,5,{scale:this.themeScale,type:this.themeType,volume:v,attack:.01,decay:.15,release:.2,delay:j*.09})}

  destroy(){if(this.ctx)try{this.ctx.close()}catch(e){}}
}

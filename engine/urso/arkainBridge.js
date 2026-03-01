/**
 * ARKAINBRAIN ↔ URSO Integration Bridge
 * 
 * This is the main orchestrator that wires ARKAINBRAIN's modules
 * (Math Model, Analytics, Free Spins, Responsible Gaming) into
 * URSO's event system and state machine.
 * 
 * Usage:
 *   import ArkainBridge from './arkainBridge.js';
 *   
 *   const bridge = new ArkainBridge({
 *       gameId: 'egyptian-gold-v1',
 *       jurisdiction: 'GB',
 *       analyticsEndpoint: 'https://api.arkainbrain.com/v1/analytics',
 *   });
 *   
 *   bridge.init(gameConfig);
 * 
 * The bridge hooks into URSO's event emitter (`Urso.observer`) to
 * intercept game events without modifying core URSO code.
 */

import MathModelController from './modules/mathModel/controller.js';
import AnalyticsController from './modules/analytics/controller.js';
import FreeSpinsController from './components/freeSpins/controller.js';
import ResponsibleGamingController from './modules/responsibleGaming/controller.js';

class ArkainBridge {
    constructor(config = {}) {
        this._config = {
            gameId: null,
            jurisdiction: 'default',
            sessionId: this._generateSessionId(),
            analyticsEndpoint: null,
            enableMathModel: true,
            enableAnalytics: true,
            enableFreeSpins: true,
            enableResponsibleGaming: true,
            ...config,
        };

        // Module instances
        this.mathModel = null;
        this.analytics = null;
        this.freeSpins = null;
        this.responsibleGaming = null;

        // URSO event subscriptions (for cleanup)
        this._subscriptions = [];
    }

    // =========================================================================
    // INITIALIZATION
    // =========================================================================

    /**
     * Initialize all ARKAINBRAIN modules and hook into URSO
     * 
     * @param {Object} gameConfig - Full game mathematical configuration
     * @param {Object} gameConfig.reelStrips - Weighted reel strip arrays per reel
     * @param {Object} gameConfig.symbolWeights - Symbol weight distribution
     * @param {Object} gameConfig.paytable - Pay table { 'symbolId-matchCount': payout }
     * @param {string} gameConfig.winType - 'lines' | 'ways' | 'cluster'
     * @param {Array}  gameConfig.paylines - Payline definitions
     * @param {Array}  gameConfig.scatterRules - Scatter pay rules
     * @param {Object} gameConfig.wildRules - Wild symbol configuration
     * @param {Object} gameConfig.freeSpinRules - Free spin trigger/award rules
     * @param {Object} gameConfig.jackpotRules - Jackpot tier definitions
     */
    init(gameConfig) {
        console.log(`[ARKAINBRAIN] Initializing bridge for ${this._config.gameId} (${this._config.jurisdiction})`);

        // --- Math Model ---
        if (this._config.enableMathModel) {
            this.mathModel = new MathModelController({
                reelsCount: gameConfig.reelsCount || 5,
                rowsCount: gameConfig.rowsCount || 3,
                targetRTP: gameConfig.targetRTP || 96.50,
                volatility: gameConfig.volatility || 'medium',
            });
            this.mathModel.init(gameConfig);
        }

        // --- Analytics ---
        if (this._config.enableAnalytics) {
            this.analytics = new AnalyticsController({
                sessionId: this._config.sessionId,
                gameId: this._config.gameId,
                jurisdiction: this._config.jurisdiction,
                endpoint: this._config.analyticsEndpoint,
            });
            this.analytics.init();
        }

        // --- Free Spins ---
        if (this._config.enableFreeSpins && gameConfig.freeSpinRules) {
            this.freeSpins = new FreeSpinsController(gameConfig.freeSpinRules);
            this._wireFreeSpinsCallbacks();
        }

        // --- Responsible Gaming ---
        if (this._config.enableResponsibleGaming) {
            this.responsibleGaming = new ResponsibleGamingController({
                jurisdiction: this._config.jurisdiction,
                lossLimit: this._config.lossLimit,
            });
            this.responsibleGaming.init();
            this._wireResponsibleGamingCallbacks();
        }

        // --- Hook into URSO events ---
        this._hookIntoURSO();

        // Inject symbol weights into URSO's localData for the weighted random fix
        if (gameConfig.symbolWeights && typeof Urso !== 'undefined') {
            Urso.localData.set('symbolWeights', gameConfig.symbolWeights);
        }

        console.log('[ARKAINBRAIN] Bridge initialized successfully');
        return this;
    }

    // =========================================================================
    // URSO EVENT HOOKS
    // =========================================================================

    _hookIntoURSO() {
        if (typeof Urso === 'undefined') {
            console.warn('[ARKAINBRAIN] Urso not found — running in standalone mode');
            return;
        }

        // Hook: Spin Start
        this._subscribe('modules.statesManager.state.START_SPIN', () => {
            const linesData = Urso.localData.get('lines');
            const coinData = Urso.localData.get('coins');
            const betData = Urso.localData.get('bets');
            const totalBet = Urso.localData.get('totalBet.value') || (betData.value * linesData.value * coinData.value);

            // Responsible gaming pre-spin check
            if (this.responsibleGaming) {
                const validation = this.responsibleGaming.validateBet(totalBet);
                if (!validation.allowed) {
                    console.warn(`[ARKAINBRAIN] Bet blocked: ${validation.reason}`);
                    // TODO: Emit URSO event to cancel spin
                    return;
                }
            }

            // Analytics: spin start
            if (this.analytics) {
                this.analytics.onSpinStart({
                    betAmount: betData.value,
                    betPerLine: betData.value,
                    linesCount: linesData.value,
                    totalBet,
                });
            }
        });

        // Hook: Spin Result Received
        this._subscribe('modules.transport.receive', (response) => {
            if (response?.type !== 'Spin') return;

            const slotData = Urso.localData.get('slotMachine');
            if (!slotData?.spinStages?.[0]) return;

            const stage = slotData.spinStages[0];
            const totalBet = Urso.localData.get('totalBet.value') || 0;

            // Analytics: spin result
            if (this.analytics) {
                this.analytics.onSpinResult({
                    matrix: stage.spinResult?.rows,
                    totalWin: stage.slotWin?.totalWin || 0,
                    lineWins: stage.slotWin?.lineWinAmounts || [],
                    scatterWins: [],
                    hitType: this._classifyWin(stage.slotWin?.totalWin || 0, totalBet),
                    multiplier: 1,
                    freeSpinsAwarded: 0,
                    bonusTriggered: null,
                    jackpotResult: null,
                    totalBet,
                });
            }

            // Responsible gaming: track spin
            if (this.responsibleGaming) {
                this.responsibleGaming.trackSpin(totalBet, stage.slotWin?.totalWin || 0);
            }

            // Free spins: check trigger
            if (this.freeSpins && stage.slotWin) {
                this.freeSpins.checkTrigger({
                    freeSpinsAwarded: stage.slotWin.freeSpinsAwarded || 0,
                    bonusTriggered: stage.slotWin.bonusTriggered || null,
                });
            }
        });

        // Hook: Bet Change
        this._subscribe('modules.logic.ui.bet.changed', (data) => {
            if (this.analytics) {
                this.analytics.onBetChange({
                    previousBet: data?.previous,
                    newBet: data?.current,
                    betType: 'bet',
                });
            }
        });

        // Hook: AutoSpin Toggle
        this._subscribe('modules.logic.ui.auto.update', () => {
            const enabled = Urso.localData.get('autospin.enabled');
            const left = Urso.localData.get('autospin.left');

            // Responsible gaming: validate autoSpin
            if (this.responsibleGaming && enabled) {
                const validation = this.responsibleGaming.validateAutoSpin(left);
                if (!validation.allowed) {
                    Urso.localData.set('autospin.enabled', false);
                    console.warn(`[ARKAINBRAIN] AutoSpin blocked: ${validation.reason}`);
                    return;
                }
            }

            if (this.analytics) {
                this.analytics.onAutoSpinToggle({ enabled, count: left });
            }
        });

        // Hook: Gamble
        this._subscribe('modules.transport.receive', (response) => {
            if (response?.type !== 'Gamble') return;

            if (this.analytics) {
                const gambleData = Urso.localData.get('gamble');
                this.analytics.onGambleAttempt({
                    stakeAmount: gambleData?.stakeAmount || 0,
                    result: gambleData?.totalWin > 0 ? 'win' : 'loss',
                    winAmount: gambleData?.totalWin || 0,
                });
            }
        });

        // Hook: Window close / session end
        if (typeof window !== 'undefined') {
            window.addEventListener('beforeunload', () => {
                if (this.analytics) {
                    this.analytics.onSessionEnd('window_close');
                }
            });
        }
    }

    /**
     * Subscribe to URSO event with cleanup tracking
     */
    _subscribe(event, callback) {
        if (typeof Urso !== 'undefined' && Urso.observer) {
            Urso.observer.add(event, callback);
            this._subscriptions.push({ event, callback });
        }
    }

    // =========================================================================
    // FREE SPINS WIRING
    // =========================================================================

    _wireFreeSpinsCallbacks() {
        this.freeSpins
            .onTrigger((data) => {
                console.log(`[ARKAINBRAIN] Free Spins triggered: ${data.totalSpins} spins at ${data.multiplier}x`);
                if (this.analytics) {
                    this.analytics.onFreeSpinTrigger({
                        totalAwarded: data.totalSpins,
                        triggerSymbolCount: 0,
                        currentMultiplier: data.multiplier,
                        isRetrigger: false,
                    });
                }
            })
            .onSpinComplete((data) => {
                if (this.analytics) {
                    this.analytics.onFreeSpinComplete({
                        spinNumber: data.spinNumber,
                        totalSpins: data.totalSpins,
                        winAmount: data.spinWin,
                        multiplier: data.multiplier,
                        remainingSpins: data.remainingSpins,
                    });
                }
            })
            .onRetrigger((data) => {
                console.log(`[ARKAINBRAIN] Free Spins retriggered: +${data.additionalSpins} spins`);
                if (this.analytics) {
                    this.analytics.onFreeSpinTrigger({
                        totalAwarded: data.additionalSpins,
                        triggerSymbolCount: data.scatterCount,
                        currentMultiplier: data.multiplier,
                        isRetrigger: true,
                    });
                }
            })
            .onComplete((data) => {
                console.log(`[ARKAINBRAIN] Free Spins complete: ${data.cumulativeWin} total win over ${data.totalSpins} spins`);
            });
    }

    // =========================================================================
    // RESPONSIBLE GAMING WIRING
    // =========================================================================

    _wireResponsibleGamingCallbacks() {
        this.responsibleGaming
            .onRealityCheck((data) => {
                console.log(`[ARKAINBRAIN] Reality Check — Session: ${Math.round(data.sessionDuration / 60000)}min, Net: ${data.netPosition}`);
                // TODO: Emit URSO event to show reality check popup
            })
            .onSessionLimit((data) => {
                console.log(`[ARKAINBRAIN] Session limit reached — ${data.cooldownMinutes}min cooldown`);
                // TODO: Emit URSO event to force session end
            })
            .onLossLimit((data) => {
                console.log(`[ARKAINBRAIN] Loss limit reached: ${data.currentLoss} / ${data.limit}`);
                // TODO: Emit URSO event to disable spin
            })
            .onBetRestricted((data) => {
                console.log(`[ARKAINBRAIN] Bet restricted: max €${data.maxAllowed}`);
                // TODO: Emit URSO event to cap bet
            })
            .onAutoSpinRestricted((data) => {
                console.log(`[ARKAINBRAIN] AutoSpin restricted: ${data.reason}`);
            });
    }

    // =========================================================================
    // PUBLIC API
    // =========================================================================

    /**
     * Process a spin through ARKAINBRAIN's math model
     * Use this instead of (or alongside) the server spin response
     */
    processSpin(betPerLine, totalBet) {
        if (!this.mathModel) return null;

        const { matrix, stopPositions } = this.mathModel.generateSpinResult();
        const multiplier = this.freeSpins?.isActive ? this.freeSpins.currentMultiplier : 1;
        const evaluation = this.mathModel.evaluateWins(matrix, betPerLine, totalBet, multiplier);

        return {
            spinResult: { rows: matrix },
            slotWin: evaluation,
            stopPositions,
            mathStats: {
                sessionRTP: this.mathModel.getSessionRTP(),
                rollingRTP: this.mathModel.getRollingRTP(),
                hitFrequency: this.mathModel.getHitFrequency(),
            },
        };
    }

    /**
     * Get comprehensive report for ARKAINBRAIN's revenue engine
     */
    getIntelligenceReport() {
        const report = {
            timestamp: new Date().toISOString(),
            gameId: this._config.gameId,
            jurisdiction: this._config.jurisdiction,
            sessionId: this._config.sessionId,
        };

        if (this.analytics) {
            report.analytics = this.analytics.generateMarketIntelligencePayload();
        }

        if (this.mathModel) {
            report.mathModel = this.mathModel.getSessionStats();
        }

        if (this.freeSpins) {
            report.freeSpins = this.freeSpins.getState();
        }

        if (this.responsibleGaming) {
            report.compliance = this.responsibleGaming.getComplianceReport();
        }

        return report;
    }

    /**
     * Get minimum spin duration for current jurisdiction
     */
    getMinSpinDuration() {
        return this.responsibleGaming?.getMinSpinDuration() || 0;
    }

    /**
     * Cleanup all subscriptions and timers
     */
    destroy() {
        // Unsubscribe from URSO events
        if (typeof Urso !== 'undefined' && Urso.observer) {
            for (const sub of this._subscriptions) {
                Urso.observer.remove(sub.event, sub.callback);
            }
        }
        this._subscriptions = [];

        // Destroy modules
        if (this.analytics) this.analytics.destroy();
        if (this.responsibleGaming) this.responsibleGaming.destroy();

        console.log('[ARKAINBRAIN] Bridge destroyed');
    }

    // =========================================================================
    // HELPERS
    // =========================================================================

    _generateSessionId() {
        return `arkain-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
    }

    _classifyWin(totalWin, totalBet) {
        if (totalWin === 0) return 'none';
        const ratio = totalWin / totalBet;
        if (ratio < 1) return 'minor';
        if (ratio < 5) return 'standard';
        if (ratio < 15) return 'big';
        if (ratio < 50) return 'mega';
        return 'super';
    }
}

export default ArkainBridge;

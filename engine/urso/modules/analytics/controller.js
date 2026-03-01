/**
 * ARKAINBRAIN Analytics Pipeline
 * 
 * Real-time telemetry system that hooks into URSO's event system
 * to capture every meaningful game event for revenue projection,
 * market intelligence, and compliance reporting.
 * 
 * Emits structured events to ARKAINBRAIN's backend via configurable
 * transport (WebSocket, REST, or batch queue).
 */

class AnalyticsController {
    constructor(config = {}) {
        this._config = {
            batchSize: 50,           // Events per batch
            flushIntervalMs: 5000,   // Auto-flush interval
            endpoint: null,          // ARKAINBRAIN analytics endpoint
            sessionId: null,
            gameId: null,
            jurisdiction: null,      // Market/jurisdiction code
            enableRealtime: true,    // WebSocket streaming
            enableBatch: true,       // Batch upload
            ...config,
        };

        this._eventQueue = [];
        this._flushTimer = null;
        this._sessionMetrics = this._createSessionMetrics();
        this._ggrTracker = this._createGGRTracker();
        this._listeners = new Map();
    }

    // =========================================================================
    // LIFECYCLE
    // =========================================================================

    init() {
        this._sessionMetrics.sessionStart = Date.now();
        this._startFlushTimer();
        return this;
    }

    destroy() {
        this._stopFlushTimer();
        this._flush(); // Final flush
        this._listeners.clear();
    }

    // =========================================================================
    // EVENT HOOKS — Plug these into URSO's event emitter
    // =========================================================================

    /**
     * Hook: Called when a spin starts
     * Attach to: statesManager START_SPIN state entry
     */
    onSpinStart(data) {
        const { betAmount, betPerLine, linesCount, totalBet, extraBet = 0 } = data;

        this._sessionMetrics.totalSpins++;
        this._sessionMetrics.totalBet += totalBet;
        this._ggrTracker.totalWagered += totalBet;

        this._emit('spin.start', {
            betAmount,
            betPerLine,
            linesCount,
            totalBet,
            extraBet,
            spinNumber: this._sessionMetrics.totalSpins,
            timestamp: Date.now(),
        });
    }

    /**
     * Hook: Called when spin result is evaluated
     * Attach to: statesManager after serverSpinRequestAction completes
     */
    onSpinResult(data) {
        const {
            matrix,
            totalWin,
            lineWins,
            scatterWins,
            hitType,
            multiplier,
            freeSpinsAwarded,
            bonusTriggered,
            jackpotResult,
            totalBet,
        } = data;

        this._sessionMetrics.totalWin += totalWin;
        this._ggrTracker.totalPaidOut += totalWin;

        if (totalWin > 0) {
            this._sessionMetrics.winSpins++;
            if (totalWin > this._sessionMetrics.maxWin) {
                this._sessionMetrics.maxWin = totalWin;
            }
        }

        // Track win distribution
        this._trackWinDistribution(hitType, totalWin);

        this._emit('spin.result', {
            matrix,
            totalWin,
            totalBet,
            lineWinCount: lineWins?.length || 0,
            scatterWinCount: scatterWins?.length || 0,
            hitType,
            multiplier,
            freeSpinsAwarded: freeSpinsAwarded || 0,
            bonusTriggered,
            jackpotTier: jackpotResult?.tier || null,
            jackpotContribution: jackpotResult?.contribution || 0,
            sessionRTP: this._calculateSessionRTP(),
            timestamp: Date.now(),
        });
    }

    /**
     * Hook: Called when free spins are triggered
     */
    onFreeSpinTrigger(data) {
        const { totalAwarded, triggerSymbolCount, currentMultiplier, isRetrigger } = data;

        this._sessionMetrics.freeSpinTriggers++;
        this._sessionMetrics.totalFreeSpins += totalAwarded;

        this._emit('freespin.trigger', {
            totalAwarded,
            triggerSymbolCount,
            currentMultiplier,
            isRetrigger,
            timestamp: Date.now(),
        });
    }

    /**
     * Hook: Called on each free spin completion
     */
    onFreeSpinComplete(data) {
        const { spinNumber, totalSpins, winAmount, multiplier, remainingSpins } = data;

        this._emit('freespin.spin', {
            spinNumber,
            totalSpins,
            winAmount,
            multiplier,
            remainingSpins,
            timestamp: Date.now(),
        });
    }

    /**
     * Hook: Called when bonus game starts
     */
    onBonusTrigger(data) {
        const { bonusType, expectedValue } = data;

        this._sessionMetrics.bonusTriggers++;

        this._emit('bonus.trigger', {
            bonusType,
            expectedValue,
            timestamp: Date.now(),
        });
    }

    /**
     * Hook: Called when gamble feature is used
     */
    onGambleAttempt(data) {
        const { stakeAmount, choice, result, winAmount } = data;

        this._sessionMetrics.gambleAttempts++;
        if (result === 'win') this._sessionMetrics.gambleWins++;

        this._emit('gamble.attempt', {
            stakeAmount,
            choice,
            result,
            winAmount,
            timestamp: Date.now(),
        });
    }

    /**
     * Hook: Called when bet level changes
     */
    onBetChange(data) {
        const { previousBet, newBet, betType } = data;

        this._emit('bet.change', {
            previousBet,
            newBet,
            betType,
            timestamp: Date.now(),
        });
    }

    /**
     * Hook: Called when autoSpin starts or stops
     */
    onAutoSpinToggle(data) {
        const { enabled, count, lossLimit } = data;

        this._emit('autospin.toggle', {
            enabled,
            count,
            lossLimit,
            timestamp: Date.now(),
        });
    }

    /**
     * Hook: Called on session end (window close, timeout, etc.)
     */
    onSessionEnd(reason = 'normal') {
        const sessionDuration = Date.now() - this._sessionMetrics.sessionStart;

        this._emit('session.end', {
            reason,
            duration: sessionDuration,
            metrics: this.getSessionSummary(),
            timestamp: Date.now(),
        });

        this._flush(); // Force final flush
    }

    // =========================================================================
    // GGR (Gross Gaming Revenue) TRACKING
    // =========================================================================

    _createGGRTracker() {
        return {
            totalWagered: 0,
            totalPaidOut: 0,
            jackpotContributions: 0,
            bonusPayouts: 0,
        };
    }

    /**
     * Get current GGR for this session
     * GGR = Total Wagered - Total Paid Out
     */
    getGGR() {
        return this._ggrTracker.totalWagered - this._ggrTracker.totalPaidOut;
    }

    /**
     * Get GGR margin (GGR as % of wagered)
     */
    getGGRMargin() {
        if (this._ggrTracker.totalWagered === 0) return 0;
        return (this.getGGR() / this._ggrTracker.totalWagered) * 100;
    }

    // =========================================================================
    // SESSION METRICS
    // =========================================================================

    _createSessionMetrics() {
        return {
            sessionStart: null,
            totalSpins: 0,
            totalBet: 0,
            totalWin: 0,
            winSpins: 0,
            maxWin: 0,
            freeSpinTriggers: 0,
            totalFreeSpins: 0,
            bonusTriggers: 0,
            gambleAttempts: 0,
            gambleWins: 0,
            winDistribution: {
                none: 0,
                minor: 0,
                standard: 0,
                big: 0,
                mega: 0,
                super: 0,
                jackpot: 0,
            },
        };
    }

    _trackWinDistribution(hitType, amount) {
        if (this._sessionMetrics.winDistribution[hitType] !== undefined) {
            this._sessionMetrics.winDistribution[hitType]++;
        }
    }

    _calculateSessionRTP() {
        if (this._sessionMetrics.totalBet === 0) return 0;
        return (this._sessionMetrics.totalWin / this._sessionMetrics.totalBet) * 100;
    }

    /**
     * Get comprehensive session summary for ARKAINBRAIN
     */
    getSessionSummary() {
        const duration = Date.now() - (this._sessionMetrics.sessionStart || Date.now());

        return {
            ...this._sessionMetrics,
            sessionDuration: duration,
            sessionRTP: this._calculateSessionRTP(),
            hitFrequency: this._sessionMetrics.totalSpins > 0
                ? (this._sessionMetrics.winSpins / this._sessionMetrics.totalSpins) * 100
                : 0,
            avgBet: this._sessionMetrics.totalSpins > 0
                ? this._sessionMetrics.totalBet / this._sessionMetrics.totalSpins
                : 0,
            spinsPerMinute: duration > 0
                ? (this._sessionMetrics.totalSpins / (duration / 60000))
                : 0,
            ggr: this.getGGR(),
            ggrMargin: this.getGGRMargin(),
            jurisdiction: this._config.jurisdiction,
            gameId: this._config.gameId,
        };
    }

    // =========================================================================
    // MARKET INTELLIGENCE EXPORT
    // =========================================================================

    /**
     * Generate market intelligence report for ARKAINBRAIN revenue engine
     * This feeds directly into the multi-engine export pipeline
     */
    generateMarketIntelligencePayload() {
        const summary = this.getSessionSummary();

        return {
            reportType: 'session_intelligence',
            version: '1.0',
            gameId: this._config.gameId,
            jurisdiction: this._config.jurisdiction,
            sessionId: this._config.sessionId,
            generatedAt: new Date().toISOString(),

            // Revenue metrics
            revenue: {
                ggr: summary.ggr,
                ggrMargin: summary.ggrMargin,
                totalWagered: summary.totalBet,
                totalPaidOut: summary.totalWin,
                avgBetSize: summary.avgBet,
            },

            // Player behavior
            behavior: {
                totalSpins: summary.totalSpins,
                sessionDuration: summary.sessionDuration,
                spinsPerMinute: summary.spinsPerMinute,
                hitFrequency: summary.hitFrequency,
                gambleUsageRate: summary.totalSpins > 0
                    ? (summary.gambleAttempts / summary.totalSpins) * 100
                    : 0,
                autoSpinUsage: false, // TODO: track autospin usage %
            },

            // Math model performance
            mathPerformance: {
                actualRTP: summary.sessionRTP,
                winDistribution: summary.winDistribution,
                maxWinMultiple: summary.maxWin > 0 && summary.avgBet > 0
                    ? summary.maxWin / summary.avgBet
                    : 0,
                freeSpinFrequency: summary.totalSpins > 0
                    ? (summary.freeSpinTriggers / summary.totalSpins) * 100
                    : 0,
                bonusFrequency: summary.totalSpins > 0
                    ? (summary.bonusTriggers / summary.totalSpins) * 100
                    : 0,
            },
        };
    }

    // =========================================================================
    // EVENT QUEUE & TRANSPORT
    // =========================================================================

    _emit(eventType, payload) {
        const event = {
            type: eventType,
            sessionId: this._config.sessionId,
            gameId: this._config.gameId,
            jurisdiction: this._config.jurisdiction,
            payload,
        };

        // Notify local listeners
        const listeners = this._listeners.get(eventType) || [];
        listeners.forEach(fn => {
            try { fn(event); } catch (e) { console.error('Analytics listener error:', e); }
        });

        // Queue for batch transport
        if (this._config.enableBatch) {
            this._eventQueue.push(event);
            if (this._eventQueue.length >= this._config.batchSize) {
                this._flush();
            }
        }
    }

    /**
     * Subscribe to analytics events locally
     * Useful for URSO components that want to react to analytics data
     */
    on(eventType, callback) {
        if (!this._listeners.has(eventType)) {
            this._listeners.set(eventType, []);
        }
        this._listeners.get(eventType).push(callback);
    }

    off(eventType, callback) {
        const listeners = this._listeners.get(eventType);
        if (listeners) {
            const idx = listeners.indexOf(callback);
            if (idx !== -1) listeners.splice(idx, 1);
        }
    }

    _startFlushTimer() {
        if (this._config.flushIntervalMs > 0) {
            this._flushTimer = setInterval(
                () => this._flush(),
                this._config.flushIntervalMs,
            );
        }
    }

    _stopFlushTimer() {
        if (this._flushTimer) {
            clearInterval(this._flushTimer);
            this._flushTimer = null;
        }
    }

    async _flush() {
        if (this._eventQueue.length === 0) return;

        const batch = [...this._eventQueue];
        this._eventQueue = [];

        if (!this._config.endpoint) {
            // No endpoint configured — log for development
            console.debug(`[ARKAINBRAIN Analytics] Flushed ${batch.length} events`);
            return;
        }

        try {
            await fetch(this._config.endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    batchId: `${this._config.sessionId}-${Date.now()}`,
                    events: batch,
                }),
            });
        } catch (err) {
            // Re-queue on failure (with cap to prevent memory bloat)
            if (this._eventQueue.length < this._config.batchSize * 10) {
                this._eventQueue.unshift(...batch);
            }
            console.error('[ARKAINBRAIN Analytics] Flush failed:', err);
        }
    }
}

export default AnalyticsController;

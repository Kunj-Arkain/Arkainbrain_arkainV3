/**
 * ARKAINBRAIN Free Spins Component
 * 
 * Manages the free spins bonus round lifecycle:
 * - Trigger detection (scatter count threshold)
 * - Spin count tracking with retrigger support
 * - Multiplier escalation (configurable per spin or per retrigger)
 * - Cumulative win tracking
 * - State integration with URSO's statesManager
 * 
 * Plugs into the state machine between SHOW_WIN and IDLE,
 * creating a FREE_SPIN loop state.
 */

class ComponentsFreeSpinsController {
    constructor(config = {}) {
        this._config = {
            // Trigger settings
            triggerSymbolId: 8,          // Scatter symbol ID
            minTriggerCount: 3,          // Minimum scatters to trigger

            // Spins awarded per scatter count
            spinsAwarded: {
                3: 10,
                4: 15,
                5: 25,
            },

            // Retrigger settings
            retriggerEnabled: true,
            retriggerMinCount: 3,
            retriggerSpins: {
                3: 5,
                4: 10,
                5: 15,
            },
            maxRetriggers: 3,           // Cap retriggers per bonus round

            // Multiplier settings
            multiplierMode: 'fixed',     // 'fixed' | 'escalating' | 'per-retrigger'
            baseMultiplier: 1,
            escalationStep: 1,           // For escalating: +1x each spin
            maxMultiplier: 10,
            retriggerMultiplierBonus: 1,  // For per-retrigger: +1x each retrigger

            // Display settings
            introAnimationDuration: 2000,
            outroAnimationDuration: 1500,
            spinDelay: 500,              // Delay between auto-spins in free spin mode

            ...config,
        };

        // Runtime state
        this._active = false;
        this._totalSpins = 0;
        this._remainingSpins = 0;
        this._currentSpin = 0;
        this._currentMultiplier = this._config.baseMultiplier;
        this._retriggerCount = 0;
        this._cumulativeWin = 0;
        this._spinHistory = [];

        // Callbacks
        this._onTrigger = null;
        this._onSpinStart = null;
        this._onSpinComplete = null;
        this._onRetrigger = null;
        this._onComplete = null;
        this._onMultiplierChange = null;
    }

    // =========================================================================
    // URSO STATE MACHINE INTEGRATION
    // =========================================================================

    /**
     * State configuration to merge into URSO's configStates
     * Add this to your game's configStates.js
     */
    static getStateConfig() {
        return {
            FREE_SPIN_INTRO: {
                sequence: [
                    { action: 'freeSpinIntroAction' },
                ],
            },

            FREE_SPIN: {
                sequence: [
                    { action: 'freeSpinStartAction' },
                    { action: 'regularSpinStartAction' },
                    { action: 'serverSpinRequestAction' },
                    { action: 'updateSlotMachineDataAction' },
                    {
                        race: [
                            { action: 'finishingSpinAction' },
                            { action: 'waitingForInteractionAction' },
                        ],
                    },
                    { action: 'freeSpinShowWinAction' },
                    { action: 'freeSpinCheckRetriggerAction' },
                    { action: 'freeSpinUpdateCounterAction' },
                ],
            },

            FREE_SPIN_OUTRO: {
                sequence: [
                    { action: 'freeSpinOutroAction' },
                    { action: 'updateBalanceAction' },
                ],
            },
        };
    }

    /**
     * Action configuration to merge into URSO's component actions
     */
    getActionConfig() {
        return {
            freeSpinIntroAction: {
                guard: () => this._active,
                run: (finishClbk) => this._runIntro(finishClbk),
            },
            freeSpinStartAction: {
                guard: () => this._active && this._remainingSpins > 0,
                run: (finishClbk) => this._runSpinStart(finishClbk),
            },
            freeSpinShowWinAction: {
                run: (finishClbk) => this._runShowWin(finishClbk),
            },
            freeSpinCheckRetriggerAction: {
                run: (finishClbk) => this._runCheckRetrigger(finishClbk),
            },
            freeSpinUpdateCounterAction: {
                run: (finishClbk) => this._runUpdateCounter(finishClbk),
            },
            freeSpinOutroAction: {
                guard: () => this._remainingSpins === 0,
                run: (finishClbk) => this._runOutro(finishClbk),
            },
        };
    }

    // =========================================================================
    // TRIGGER DETECTION
    // =========================================================================

    /**
     * Check if free spins should be triggered from a spin result
     * Call this after win evaluation in SHOW_WIN state
     * 
     * @param {Object} spinResult - The evaluated spin result
     * @returns {boolean} Whether free spins were triggered
     */
    checkTrigger(spinResult) {
        const { freeSpinsAwarded, bonusTriggered } = spinResult;

        if (freeSpinsAwarded > 0 && bonusTriggered === 'freeSpins') {
            this.trigger(freeSpinsAwarded);
            return true;
        }

        return false;
    }

    /**
     * Manually trigger free spins (e.g., from pick bonus)
     * @param {number} spinsCount - Number of free spins to award
     * @param {number} initialMultiplier - Starting multiplier
     */
    trigger(spinsCount, initialMultiplier = null) {
        this._active = true;
        this._totalSpins = spinsCount;
        this._remainingSpins = spinsCount;
        this._currentSpin = 0;
        this._currentMultiplier = initialMultiplier || this._config.baseMultiplier;
        this._retriggerCount = 0;
        this._cumulativeWin = 0;
        this._spinHistory = [];

        if (this._onTrigger) {
            this._onTrigger({
                totalSpins: this._totalSpins,
                multiplier: this._currentMultiplier,
            });
        }
    }

    // =========================================================================
    // SPIN LIFECYCLE
    // =========================================================================

    _runIntro(finishClbk) {
        // Emit event for UI to show free spins intro animation
        if (this._onTrigger) {
            this._onTrigger({
                totalSpins: this._totalSpins,
                multiplier: this._currentMultiplier,
            });
        }

        setTimeout(() => {
            finishClbk();
        }, this._config.introAnimationDuration);
    }

    _runSpinStart(finishClbk) {
        this._currentSpin++;
        this._remainingSpins--;

        // Update multiplier based on mode
        this._updateMultiplier();

        if (this._onSpinStart) {
            this._onSpinStart({
                currentSpin: this._currentSpin,
                totalSpins: this._totalSpins,
                remainingSpins: this._remainingSpins,
                multiplier: this._currentMultiplier,
                cumulativeWin: this._cumulativeWin,
            });
        }

        finishClbk();
    }

    _runShowWin(finishClbk) {
        // This runs after the spin result is in — accumulate win
        // Access win data from URSO's localData (or pass via event)
        const spinWin = this._getLastSpinWin();
        const multipliedWin = spinWin * this._currentMultiplier;
        this._cumulativeWin += multipliedWin;

        this._spinHistory.push({
            spin: this._currentSpin,
            baseWin: spinWin,
            multiplier: this._currentMultiplier,
            totalWin: multipliedWin,
            cumulativeWin: this._cumulativeWin,
        });

        if (this._onSpinComplete) {
            this._onSpinComplete({
                spinNumber: this._currentSpin,
                totalSpins: this._totalSpins,
                remainingSpins: this._remainingSpins,
                spinWin: multipliedWin,
                baseWin: spinWin,
                multiplier: this._currentMultiplier,
                cumulativeWin: this._cumulativeWin,
            });
        }

        // Delay before next spin (auto-advance in free spin mode)
        setTimeout(() => finishClbk(), this._config.spinDelay);
    }

    _runCheckRetrigger(finishClbk) {
        if (!this._config.retriggerEnabled) {
            finishClbk();
            return;
        }

        if (this._retriggerCount >= this._config.maxRetriggers) {
            finishClbk();
            return;
        }

        const matrix = this._getCurrentMatrix();
        if (!matrix) {
            finishClbk();
            return;
        }

        const scatterCount = this._countSymbol(matrix, this._config.triggerSymbolId);

        if (scatterCount >= this._config.retriggerMinCount) {
            const additionalSpins = this._config.retriggerSpins[scatterCount]
                || this._config.retriggerSpins[this._config.retriggerMinCount]
                || 0;

            if (additionalSpins > 0) {
                this._retriggerCount++;
                this._remainingSpins += additionalSpins;
                this._totalSpins += additionalSpins;

                // Apply retrigger multiplier bonus
                if (this._config.multiplierMode === 'per-retrigger') {
                    this._currentMultiplier += this._config.retriggerMultiplierBonus;
                    this._currentMultiplier = Math.min(
                        this._currentMultiplier,
                        this._config.maxMultiplier,
                    );
                }

                if (this._onRetrigger) {
                    this._onRetrigger({
                        additionalSpins,
                        scatterCount,
                        newTotal: this._totalSpins,
                        remaining: this._remainingSpins,
                        retriggerNumber: this._retriggerCount,
                        multiplier: this._currentMultiplier,
                    });
                }
            }
        }

        finishClbk();
    }

    _runUpdateCounter(finishClbk) {
        // Check if free spins are complete
        if (this._remainingSpins <= 0) {
            this._active = false;
            // State machine should transition to FREE_SPIN_OUTRO
        }
        // Otherwise, state machine loops back to FREE_SPIN

        finishClbk();
    }

    _runOutro(finishClbk) {
        if (this._onComplete) {
            this._onComplete({
                totalSpins: this._currentSpin,
                cumulativeWin: this._cumulativeWin,
                retriggerCount: this._retriggerCount,
                spinHistory: this._spinHistory,
                finalMultiplier: this._currentMultiplier,
            });
        }

        setTimeout(() => {
            this._reset();
            finishClbk();
        }, this._config.outroAnimationDuration);
    }

    // =========================================================================
    // MULTIPLIER SYSTEM
    // =========================================================================

    _updateMultiplier() {
        const { multiplierMode, escalationStep, maxMultiplier } = this._config;

        switch (multiplierMode) {
            case 'escalating':
                this._currentMultiplier = Math.min(
                    this._config.baseMultiplier + (this._currentSpin - 1) * escalationStep,
                    maxMultiplier,
                );
                break;

            case 'per-retrigger':
                // Multiplier only changes on retrigger (handled in _runCheckRetrigger)
                break;

            case 'fixed':
            default:
                // No change
                break;
        }

        if (this._onMultiplierChange) {
            this._onMultiplierChange(this._currentMultiplier);
        }
    }

    // =========================================================================
    // HELPERS
    // =========================================================================

    /**
     * Get the win from the last spin result
     * Override this method to match your URSO localData structure
     */
    _getLastSpinWin() {
        // Default: read from URSO's localData
        if (typeof Urso !== 'undefined') {
            const slotData = Urso.localData.get('slotMachine');
            if (slotData?.spinStages?.[0]?.slotWin?.totalWin) {
                return slotData.spinStages[0].slotWin.totalWin;
            }
        }
        return 0;
    }

    /**
     * Get current spin matrix from URSO's localData
     */
    _getCurrentMatrix() {
        if (typeof Urso !== 'undefined') {
            return Urso.localData.get('slotMachine.spinStages.0.spinResult.rows');
        }
        return null;
    }

    /**
     * Count occurrences of a symbol in the matrix
     */
    _countSymbol(matrix, symbolId) {
        let count = 0;
        for (const row of matrix) {
            for (const sym of row) {
                if (sym === symbolId) count++;
            }
        }
        return count;
    }

    _reset() {
        this._active = false;
        this._totalSpins = 0;
        this._remainingSpins = 0;
        this._currentSpin = 0;
        this._currentMultiplier = this._config.baseMultiplier;
        this._retriggerCount = 0;
        this._cumulativeWin = 0;
        this._spinHistory = [];
    }

    // =========================================================================
    // PUBLIC STATE ACCESSORS
    // =========================================================================

    get isActive() { return this._active; }
    get currentSpin() { return this._currentSpin; }
    get totalSpins() { return this._totalSpins; }
    get remainingSpins() { return this._remainingSpins; }
    get currentMultiplier() { return this._currentMultiplier; }
    get cumulativeWin() { return this._cumulativeWin; }
    get retriggerCount() { return this._retriggerCount; }
    get spinHistory() { return [...this._spinHistory]; }

    /**
     * Get complete state snapshot (for ARKAINBRAIN analytics)
     */
    getState() {
        return {
            active: this._active,
            currentSpin: this._currentSpin,
            totalSpins: this._totalSpins,
            remainingSpins: this._remainingSpins,
            currentMultiplier: this._currentMultiplier,
            retriggerCount: this._retriggerCount,
            cumulativeWin: this._cumulativeWin,
            config: { ...this._config },
        };
    }

    // =========================================================================
    // EVENT CALLBACKS (set by integrating code)
    // =========================================================================

    onTrigger(fn) { this._onTrigger = fn; return this; }
    onSpinStart(fn) { this._onSpinStart = fn; return this; }
    onSpinComplete(fn) { this._onSpinComplete = fn; return this; }
    onRetrigger(fn) { this._onRetrigger = fn; return this; }
    onComplete(fn) { this._onComplete = fn; return this; }
    onMultiplierChange(fn) { this._onMultiplierChange = fn; return this; }
}

export default ComponentsFreeSpinsController;

/**
 * ARKAINBRAIN Responsible Gaming Module
 * 
 * Jurisdiction-aware compliance engine that enforces:
 * - Session time limits
 * - Loss limits
 * - Reality check intervals
 * - AutoSpin restrictions
 * - Bet caps
 * - Cool-down periods
 * 
 * Configuration profiles per jurisdiction feed from ARKAINBRAIN's
 * market intelligence database.
 */

class ResponsibleGamingController {
    constructor(config = {}) {
        this._config = {
            jurisdiction: 'default',
            ...config,
        };

        this._sessionStart = Date.now();
        this._totalDeposits = 0;
        this._totalWithdrawals = 0;
        this._netLoss = 0;
        this._spinCount = 0;
        this._realityCheckTimer = null;
        this._sessionTimer = null;
        this._cooldownActive = false;

        // Callbacks
        this._onRealityCheck = null;
        this._onSessionLimit = null;
        this._onLossLimit = null;
        this._onBetRestricted = null;
        this._onAutoSpinRestricted = null;

        // Load jurisdiction profile
        this._profile = this._getJurisdictionProfile(this._config.jurisdiction);
    }

    // =========================================================================
    // JURISDICTION PROFILES
    // =========================================================================

    _getJurisdictionProfile(jurisdiction) {
        const profiles = {
            // Swedish Gambling Authority (Spelinspektionen)
            'SE': {
                sessionTimeLimitMinutes: 60,
                mandatoryCooldownMinutes: 15,
                realityCheckIntervalMinutes: 30,
                lossLimitEnabled: true,
                maxBetEUR: null,
                autoSpinMaxCount: 0, // AutoSpin banned in Sweden
                autoSpinLossLimitRequired: true,
                depositLimitsRequired: true,
                panicButtonRequired: true,
                playBreakReminder: true,
            },

            // UK Gambling Commission (UKGC)
            'GB': {
                sessionTimeLimitMinutes: null,
                mandatoryCooldownMinutes: null,
                realityCheckIntervalMinutes: 60,
                lossLimitEnabled: true,
                maxBetEUR: null,
                autoSpinMaxCount: null,
                autoSpinLossLimitRequired: true,
                autoSpinSingleWinLimitRequired: true,
                depositLimitsRequired: true,
                panicButtonRequired: true,
                spinSpeedMinMs: 2500, // Min 2.5s per spin
                reverseWithdrawalsProhibited: true,
            },

            // Malta Gaming Authority (MGA)
            'MT': {
                sessionTimeLimitMinutes: null,
                mandatoryCooldownMinutes: null,
                realityCheckIntervalMinutes: 60,
                lossLimitEnabled: true,
                maxBetEUR: null,
                autoSpinMaxCount: null,
                autoSpinLossLimitRequired: false,
                depositLimitsRequired: true,
                panicButtonRequired: false,
            },

            // Germany (GlüStV 2021)
            'DE': {
                sessionTimeLimitMinutes: null,
                mandatoryCooldownMinutes: 5, // After 60 min
                realityCheckIntervalMinutes: 60,
                lossLimitEnabled: true,
                monthlyDepositLimitEUR: 1000,
                maxBetEUR: 1.00, // €1 max stake
                autoSpinMaxCount: 0, // AutoSpin banned
                autoSpinLossLimitRequired: true,
                depositLimitsRequired: true,
                panicButtonRequired: true,
                spinSpeedMinMs: 5000, // Min 5s per spin
                simultaneousPlayProhibited: true,
            },

            // Spain (DGOJ)
            'ES': {
                sessionTimeLimitMinutes: null,
                mandatoryCooldownMinutes: null,
                realityCheckIntervalMinutes: 30,
                lossLimitEnabled: true,
                maxBetEUR: null,
                autoSpinMaxCount: null,
                autoSpinLossLimitRequired: false,
                depositLimitsRequired: true,
                panicButtonRequired: false,
                sessionActivityReportRequired: true,
            },

            // Default (minimal compliance)
            'default': {
                sessionTimeLimitMinutes: null,
                mandatoryCooldownMinutes: null,
                realityCheckIntervalMinutes: 60,
                lossLimitEnabled: false,
                maxBetEUR: null,
                autoSpinMaxCount: null,
                autoSpinLossLimitRequired: false,
                depositLimitsRequired: false,
                panicButtonRequired: false,
            },
        };

        return profiles[jurisdiction] || profiles['default'];
    }

    // =========================================================================
    // INITIALIZATION
    // =========================================================================

    init() {
        this._sessionStart = Date.now();
        this._startTimers();
        return this;
    }

    destroy() {
        this._clearTimers();
    }

    _startTimers() {
        const { realityCheckIntervalMinutes, sessionTimeLimitMinutes } = this._profile;

        if (realityCheckIntervalMinutes) {
            this._realityCheckTimer = setInterval(
                () => this._triggerRealityCheck(),
                realityCheckIntervalMinutes * 60 * 1000,
            );
        }

        if (sessionTimeLimitMinutes) {
            this._sessionTimer = setTimeout(
                () => this._triggerSessionLimit(),
                sessionTimeLimitMinutes * 60 * 1000,
            );
        }
    }

    _clearTimers() {
        if (this._realityCheckTimer) clearInterval(this._realityCheckTimer);
        if (this._sessionTimer) clearTimeout(this._sessionTimer);
    }

    // =========================================================================
    // PRE-SPIN VALIDATION
    // =========================================================================

    /**
     * Validate a bet before allowing spin
     * Returns { allowed: boolean, reason?: string, maxAllowed?: number }
     */
    validateBet(betAmount, currency = 'EUR') {
        // Check max bet restriction (e.g., Germany €1 limit)
        if (this._profile.maxBetEUR) {
            const betInEUR = this._convertToEUR(betAmount, currency);
            if (betInEUR > this._profile.maxBetEUR) {
                if (this._onBetRestricted) {
                    this._onBetRestricted({
                        requested: betAmount,
                        maxAllowed: this._profile.maxBetEUR,
                        reason: 'jurisdiction_max_bet',
                    });
                }
                return {
                    allowed: false,
                    reason: `Maximum bet in ${this._config.jurisdiction} is €${this._profile.maxBetEUR}`,
                    maxAllowed: this._profile.maxBetEUR,
                };
            }
        }

        // Check loss limit
        if (this._profile.lossLimitEnabled && this._config.lossLimit) {
            if (this._netLoss + betAmount > this._config.lossLimit) {
                if (this._onLossLimit) {
                    this._onLossLimit({
                        currentLoss: this._netLoss,
                        limit: this._config.lossLimit,
                    });
                }
                return {
                    allowed: false,
                    reason: 'Loss limit reached',
                };
            }
        }

        // Check cooldown
        if (this._cooldownActive) {
            return {
                allowed: false,
                reason: 'Mandatory cool-down period active',
            };
        }

        return { allowed: true };
    }

    /**
     * Validate autoSpin settings
     * Returns { allowed: boolean, maxCount?: number, reason?: string }
     */
    validateAutoSpin(requestedCount, lossLimit = null) {
        const { autoSpinMaxCount, autoSpinLossLimitRequired } = this._profile;

        // AutoSpin banned entirely
        if (autoSpinMaxCount === 0) {
            if (this._onAutoSpinRestricted) {
                this._onAutoSpinRestricted({ reason: 'jurisdiction_banned' });
            }
            return {
                allowed: false,
                reason: `AutoSpin is not permitted in ${this._config.jurisdiction}`,
            };
        }

        // AutoSpin count cap
        if (autoSpinMaxCount && requestedCount > autoSpinMaxCount) {
            return {
                allowed: true,
                maxCount: autoSpinMaxCount,
                reason: `AutoSpin capped at ${autoSpinMaxCount} in ${this._config.jurisdiction}`,
            };
        }

        // Loss limit required for autoSpin
        if (autoSpinLossLimitRequired && !lossLimit) {
            return {
                allowed: false,
                reason: 'Loss limit must be set to use AutoSpin',
            };
        }

        return { allowed: true };
    }

    /**
     * Get minimum spin speed (enforced by some jurisdictions)
     */
    getMinSpinDuration() {
        return this._profile.spinSpeedMinMs || 0;
    }

    // =========================================================================
    // TRACKING
    // =========================================================================

    trackSpin(betAmount, winAmount) {
        this._spinCount++;
        this._netLoss += (betAmount - winAmount);
    }

    trackDeposit(amount) {
        this._totalDeposits += amount;

        if (this._profile.monthlyDepositLimitEUR) {
            if (this._totalDeposits > this._profile.monthlyDepositLimitEUR) {
                return {
                    allowed: false,
                    reason: `Monthly deposit limit of €${this._profile.monthlyDepositLimitEUR} reached`,
                };
            }
        }

        return { allowed: true };
    }

    // =========================================================================
    // REALITY CHECK & SESSION LIMITS
    // =========================================================================

    _triggerRealityCheck() {
        const sessionDuration = Date.now() - this._sessionStart;

        const checkData = {
            sessionDuration,
            spinCount: this._spinCount,
            netPosition: -this._netLoss,
            totalDeposits: this._totalDeposits,
            jurisdiction: this._config.jurisdiction,
        };

        if (this._onRealityCheck) {
            this._onRealityCheck(checkData);
        }

        return checkData;
    }

    _triggerSessionLimit() {
        const { mandatoryCooldownMinutes } = this._profile;

        if (this._onSessionLimit) {
            this._onSessionLimit({
                sessionDuration: Date.now() - this._sessionStart,
                cooldownMinutes: mandatoryCooldownMinutes || 0,
            });
        }

        if (mandatoryCooldownMinutes) {
            this._cooldownActive = true;
            setTimeout(() => {
                this._cooldownActive = false;
            }, mandatoryCooldownMinutes * 60 * 1000);
        }
    }

    /**
     * Panic button — immediately end session
     */
    panicStop() {
        this._cooldownActive = true;
        this._clearTimers();

        return {
            action: 'session_terminated',
            netPosition: -this._netLoss,
            sessionDuration: Date.now() - this._sessionStart,
        };
    }

    // =========================================================================
    // HELPERS
    // =========================================================================

    _convertToEUR(amount, currency) {
        // Simplified — in production, use real exchange rates
        const rates = { EUR: 1, GBP: 1.16, USD: 0.92, SEK: 0.088 };
        return amount * (rates[currency] || 1);
    }

    /**
     * Get compliance report for ARKAINBRAIN
     */
    getComplianceReport() {
        return {
            jurisdiction: this._config.jurisdiction,
            profile: this._profile,
            sessionDuration: Date.now() - this._sessionStart,
            spinCount: this._spinCount,
            netLoss: this._netLoss,
            cooldownActive: this._cooldownActive,
            restrictions: {
                maxBet: this._profile.maxBetEUR,
                autoSpinAllowed: this._profile.autoSpinMaxCount !== 0,
                autoSpinMaxCount: this._profile.autoSpinMaxCount,
                minSpinSpeed: this._profile.spinSpeedMinMs,
            },
        };
    }

    // =========================================================================
    // EVENT CALLBACKS
    // =========================================================================

    onRealityCheck(fn) { this._onRealityCheck = fn; return this; }
    onSessionLimit(fn) { this._onSessionLimit = fn; return this; }
    onLossLimit(fn) { this._onLossLimit = fn; return this; }
    onBetRestricted(fn) { this._onBetRestricted = fn; return this; }
    onAutoSpinRestricted(fn) { this._onAutoSpinRestricted = fn; return this; }
}

export default ResponsibleGamingController;

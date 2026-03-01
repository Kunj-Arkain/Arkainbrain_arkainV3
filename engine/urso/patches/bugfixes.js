/**
 * URSO Slot Base — Critical Bug Fixes
 * 
 * Apply these patches to the original URSO source files.
 * Each fix is documented with the file path, the problem, and the solution.
 */

// =============================================================================
// FIX 1: Gamble Controller — Both red/black set selectedIndex to 0
// File: src/js/components/gamble/controller.js
// =============================================================================

/*
BEFORE (BROKEN):
    _updateSelectedIndex(classes) {
        const classesArr = classes.split(' ');
        if (classesArr.includes('red')) {
            this._selectedIndex = 0;
        } else if (classesArr.includes('black')) {
            this._selectedIndex = 0;  // ← BUG: Same as red!
        }
    }

AFTER (FIXED):
*/
const gambleControllerFix = {
    file: 'src/js/components/gamble/controller.js',
    method: '_updateSelectedIndex',
    fixed: `
    _updateSelectedIndex(classes) {
        const classesArr = classes.split(' ');

        if (classesArr.includes('red')) {
            this._selectedIndex = 0;
        } else if (classesArr.includes('black')) {
            this._selectedIndex = 1;  // FIXED: black = index 1
        } else {
            Urso.logger.error('ComponentsGambleController: Undefined button was pressed!');
        }
    }`,
};


// =============================================================================
// FIX 2: Tween Memory Leak — Completed tweens never garbage collected
// File: src/js/components/slotMachine/tween.js
// =============================================================================

/*
BEFORE (LEAKS):
    _update() {
        // ...
        for (const k in this._tweens) {
            if (this._tweens[k]._complete) {
                continue; // todo remove old Tweens to garbage collect them
            }
        }
    }

AFTER (FIXED):
*/
const tweenMemoryLeakFix = {
    file: 'src/js/components/slotMachine/tween.js',
    method: '_update',
    fixed: `
    _update() {
        const curTime = (new Date()).getTime();

        if (this.gamePaused) {
            const delta = curTime - this._currentTime;
            for (const k in this._tweens) {
                if (this._tweens[k].points && this._tweens[k].points[0]) {
                    this._tweens[k].points[0].timeStart += delta;
                }
            }
            return true;
        }

        this._currentTime = curTime;

        // Collect completed tweens for cleanup
        const completedKeys = [];

        for (const k in this._tweens) {
            if (this._tweens[k]._complete) {
                completedKeys.push(k);
                continue;
            } else if (this._tweens[k]._started && !this._tweens[k]._paused) {
                this._calcStep(this._tweens[k]);
            }
        }

        // FIXED: Clean up completed tweens to prevent memory leak
        for (const key of completedKeys) {
            if (!this._tweens[key].enableUpdate) {
                delete this._tweens[key];
            }
        }

        return true;
    }`,
};


// =============================================================================
// FIX 3: configStates.js — Uncomment and harden server initialization
// File: src/js/modules/statesManager/configStates.js
// =============================================================================

const configStatesFix = {
    file: 'src/js/modules/statesManager/configStates.js',
    section: 'INIT_GAME',
    fixed: `
    INIT_GAME: {
        sequence: [
            {
                all: [
                    {
                        sequence: [
                            { action: 'updateServerSettingsAction' },
                            { action: 'transportInitAction' },
                            { action: 'serverApiVersionRequestAction' },
                            { action: 'serverCheckBrokenGameRequestAction' },
                            { action: 'serverAuthRequestAction' },
                            { action: 'serverBalanceRequestAction' },
                        ],
                    },
                    {
                        sequence: [
                            { action: 'loadDefaultSceneAction' },
                            { action: 'finishGameInitAction' },
                        ],
                    },
                ],
            },
            {
                all: [
                    { action: 'initUiLogicAction' },
                    { action: 'updateBalanceAction' },
                    { action: 'updateBetLinesAction' },
                    { action: 'hideLoaderAction' },
                ],
            },
        ],
    },`,
};


// =============================================================================
// FIX 4: PickBonus — Replace setTimeout with state-driven delay
// File: src/js/components/pickBonus/controller.js
// =============================================================================

/*
BEFORE (RACE CONDITION):
    _updateState(win) {
        this._selectedPickItem.winTextValue = win;
        this._selectedPickItem.loose = win === 0;
        setTimeout(() => {  // ← Breaks if tab is backgrounded
            if (win === 0) { ... }
        }, 2000);
    }

AFTER (FIXED with requestAnimationFrame + timestamp):
*/
const pickBonusTimerFix = {
    file: 'src/js/components/pickBonus/controller.js',
    method: '_updateState',
    fixed: `
    _updateState(win) {
        this._selectedPickItem.winTextValue = win;
        this._selectedPickItem.loose = win === 0;

        // Use state-driven delay instead of setTimeout
        // This respects tab backgrounding and game pause
        const delayMs = 2000;
        const startTime = performance.now();

        const checkDelay = () => {
            if (performance.now() - startTime >= delayMs) {
                if (win === 0) {
                    this.emit('components.winField.setText', 'LOOSE');
                } else {
                    this.emit('components.winField.setText', win);
                }
                this.emit('modules.logic.main.balanceRequest');
            } else {
                requestAnimationFrame(checkDelay);
            }
        };

        requestAnimationFrame(checkDelay);
    }`,
};


// =============================================================================
// FIX 5: Symbol Random Generation — Use weighted distribution
// File: src/js/components/slotMachine/service.js
// =============================================================================

/*
BEFORE (UNIFORM — ignores symbol rarity):
    _getRandomSymbol() {
        const symbolsArray = this._getSymbolsKeysArray();
        return this._getRandomIndexFromArray(symbolsArray);
    }

AFTER (WEIGHTED):
*/
const weightedRandomSymbolFix = {
    file: 'src/js/components/slotMachine/service.js',
    methods: ['_getRandomSymbol'],
    fixed: `
    /**
     * Symbol weight map — configure per game.
     * Higher weight = more frequent appearance on border/decoration symbols.
     * These only affect visual generation; actual spin results come from server.
     */
    _defaultSymbolWeights = null;

    _getSymbolWeights() {
        if (!this._defaultSymbolWeights) {
            // Default: use server-provided weights or fall back to uniform
            const weights = Urso.localData.get('symbolWeights');
            if (weights) {
                this._defaultSymbolWeights = weights;
            } else {
                // Fallback: uniform weights
                const symbols = this._getSymbolsKeysArray();
                this._defaultSymbolWeights = {};
                symbols.forEach(key => {
                    this._defaultSymbolWeights[key] = 1;
                });
            }
        }
        return this._defaultSymbolWeights;
    }

    _getRandomSymbol() {
        const weights = this._getSymbolWeights();
        const entries = Object.entries(weights);
        const totalWeight = entries.reduce((sum, [, w]) => sum + w, 0);
        
        let roll = Math.random() * totalWeight;
        
        for (const [key, weight] of entries) {
            roll -= weight;
            if (roll <= 0) {
                return parseInt(key);
            }
        }
        
        // Fallback
        return parseInt(entries[entries.length - 1][0]);
    }`,
};


// =============================================================================
// SUMMARY: Apply all fixes
// =============================================================================

export const fixes = {
    gambleController: gambleControllerFix,
    tweenMemoryLeak: tweenMemoryLeakFix,
    configStates: configStatesFix,
    pickBonusTimer: pickBonusTimerFix,
    weightedRandomSymbol: weightedRandomSymbolFix,
};

export default fixes;

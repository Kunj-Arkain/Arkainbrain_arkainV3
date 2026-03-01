/**
 * ARKAINBRAIN Math Model Module
 * 
 * Core mathematical engine for slot game calculations.
 * Handles: weighted reel strips, RTP computation, volatility profiling,
 * hit frequency analysis, and win evaluation (lines, ways, scatters).
 * 
 * Designed to plug into URSO's transport layer as the server-side
 * math authority — all spin results flow through here.
 */

class MathModelController {
    constructor(config = {}) {
        this._config = {
            reelsCount: 5,
            rowsCount: 3,
            targetRTP: 96.50,
            volatility: 'medium', // low | medium | high | very-high
            maxWinMultiplier: 5000,
            ...config,
        };

        this._reelStrips = [];
        this._symbolWeights = {};
        this._paytable = {};
        this._winEvaluator = null;
        this._sessionStats = this._createSessionStats();

        // RTP tracking window (rolling)
        this._rtpWindow = [];
        this._rtpWindowSize = 10000; // spins
    }

    // =========================================================================
    // INITIALIZATION
    // =========================================================================

    /**
     * Initialize math model with full game configuration
     * @param {Object} gameConfig - Complete game math specification
     */
    init(gameConfig) {
        const {
            reelStrips,
            symbolWeights,
            paytable,
            winType = 'lines',  // 'lines' | 'ways' | 'cluster'
            paylines = [],
            scatterRules = [],
            wildRules = {},
            freeSpinRules = {},
            jackpotRules = {},
        } = gameConfig;

        this._reelStrips = reelStrips;
        this._symbolWeights = symbolWeights;
        this._paytable = paytable;
        this._paylines = paylines;
        this._scatterRules = scatterRules;
        this._wildRules = wildRules;
        this._freeSpinRules = freeSpinRules;
        this._jackpotRules = jackpotRules;

        // Select win evaluator based on game type
        this._winEvaluator = this._createWinEvaluator(winType);

        return this;
    }

    // =========================================================================
    // WEIGHTED REEL STRIPS
    // =========================================================================

    /**
     * Generate spin result using weighted reel strips
     * Each reel has a defined strip sequence. We pick a random stop position
     * and read `rowsCount` symbols from that position (wrapping around).
     * 
     * @returns {Array<Array<number>>} rows × reels matrix of symbol IDs
     */
    generateSpinResult() {
        const { reelsCount, rowsCount } = this._config;
        const matrix = [];

        for (let row = 0; row < rowsCount; row++) {
            matrix.push(new Array(reelsCount).fill(0));
        }

        const stopPositions = [];

        for (let reel = 0; reel < reelsCount; reel++) {
            const strip = this._reelStrips[reel];
            if (!strip || strip.length === 0) {
                throw new Error(`MathModel: No reel strip defined for reel ${reel}`);
            }

            // Cryptographically suitable random stop position
            const stopPos = this._secureRandomInt(0, strip.length - 1);
            stopPositions.push(stopPos);

            for (let row = 0; row < rowsCount; row++) {
                const stripIndex = (stopPos + row) % strip.length;
                matrix[row][reel] = strip[stripIndex];
            }
        }

        return { matrix, stopPositions };
    }

    /**
     * Generate weighted random symbol (for border/decoration symbols)
     * Respects symbol weight distribution unlike URSO's uniform random
     */
    generateWeightedRandomSymbol() {
        const symbols = Object.keys(this._symbolWeights);
        const weights = symbols.map(s => this._symbolWeights[s].weight);
        const totalWeight = weights.reduce((sum, w) => sum + w, 0);

        let random = this._secureRandomInt(0, totalWeight - 1);

        for (let i = 0; i < symbols.length; i++) {
            random -= weights[i];
            if (random < 0) {
                return parseInt(symbols[i]);
            }
        }

        return parseInt(symbols[symbols.length - 1]);
    }

    /**
     * Generate a full border symbol matrix using weighted distribution
     */
    generateWeightedBorderSymbols(reelsCount, borderRows) {
        const result = [];
        for (let row = 0; row < borderRows; row++) {
            const rowData = [];
            for (let reel = 0; reel < reelsCount; reel++) {
                rowData.push(this.generateWeightedRandomSymbol());
            }
            result.push(rowData);
        }
        return result;
    }

    // =========================================================================
    // WIN EVALUATION ENGINE
    // =========================================================================

    /**
     * Evaluate all wins for a given spin result
     * @param {Array<Array<number>>} matrix - Symbol matrix (rows × reels)
     * @param {number} betPerLine - Bet amount per line
     * @param {number} totalBet - Total bet amount
     * @param {number} multiplier - Global multiplier (e.g. from free spins)
     * @returns {Object} Complete win evaluation result
     */
    evaluateWins(matrix, betPerLine, totalBet, multiplier = 1) {
        const lineWins = this._winEvaluator(matrix, betPerLine, multiplier);
        const scatterWins = this._evaluateScatterWins(matrix, totalBet, multiplier);
        const freeSpinTrigger = this._evaluateFreeSpinTrigger(matrix);
        const jackpotResult = this._evaluateJackpot(totalBet);

        const totalLineWin = lineWins.reduce((sum, w) => sum + w.winAmount, 0);
        const totalScatterWin = scatterWins.reduce((sum, w) => sum + w.winAmount, 0);
        const totalWin = totalLineWin + totalScatterWin + (jackpotResult?.winAmount || 0);

        // Track for RTP calculation
        this._trackSpin(totalBet, totalWin);

        return {
            lineWinAmounts: lineWins.map(w => ({
                selectedLine: w.lineIndex,
                wonSymbols: w.wonPositions,
                winAmount: w.winAmount,
                symbolKey: w.symbolKey,
                matchCount: w.matchCount,
            })),
            scatterWins,
            totalWin,
            multiplier,
            canGamble: totalWin > 0 && totalWin <= totalBet * 10,
            freeSpinsAwarded: freeSpinTrigger.awarded,
            freeSpinRetrigger: freeSpinTrigger.retrigger,
            bonusTriggered: freeSpinTrigger.bonusType,
            jackpotResult,
            hitType: this._classifyWin(totalWin, totalBet),
        };
    }

    /**
     * Create win evaluator function based on game type
     */
    _createWinEvaluator(winType) {
        switch (winType) {
            case 'lines':
                return (matrix, bet, mult) => this._evaluateLineWins(matrix, bet, mult);
            case 'ways':
                return (matrix, bet, mult) => this._evaluateWaysWins(matrix, bet, mult);
            case 'cluster':
                return (matrix, bet, mult) => this._evaluateClusterWins(matrix, bet, mult);
            default:
                return (matrix, bet, mult) => this._evaluateLineWins(matrix, bet, mult);
        }
    }

    /**
     * Fixed payline win evaluation (standard 5-reel slots)
     */
    _evaluateLineWins(matrix, betPerLine, multiplier = 1) {
        const wins = [];
        const { reelsCount } = this._config;

        for (let lineIdx = 0; lineIdx < this._paylines.length; lineIdx++) {
            const line = this._paylines[lineIdx];
            const lineSymbols = line.map((rowIdx, reelIdx) => matrix[rowIdx][reelIdx]);

            // Get the first non-wild symbol (left to right)
            let paySymbol = null;
            let matchCount = 0;
            const wonPositions = [];

            for (let reel = 0; reel < reelsCount; reel++) {
                const sym = lineSymbols[reel];
                const isWild = this._isWild(sym);

                if (paySymbol === null && !isWild) {
                    paySymbol = sym;
                }

                if (isWild || sym === paySymbol) {
                    matchCount++;
                    wonPositions.push([reel, line[reel]]);
                } else {
                    break;
                }
            }

            // All wilds on the line
            if (paySymbol === null && matchCount > 0) {
                paySymbol = this._wildRules.symbolId;
            }

            if (matchCount >= 2 && paySymbol !== null) {
                const payKey = `${paySymbol}-${matchCount}`;
                const basePay = this._paytable[payKey] || 0;

                if (basePay > 0) {
                    const wildMultiplier = this._calculateWildMultiplier(lineSymbols, matchCount);
                    const winAmount = basePay * betPerLine * multiplier * wildMultiplier;

                    wins.push({
                        lineIndex: lineIdx,
                        symbolKey: paySymbol,
                        matchCount,
                        wonPositions,
                        winAmount,
                        wildMultiplier,
                    });
                }
            }
        }

        return wins;
    }

    /**
     * Ways-to-win evaluation (243 ways, 1024 ways, etc.)
     * Any matching symbol on adjacent reels from left to right
     */
    _evaluateWaysWins(matrix, betPerLine, multiplier = 1) {
        const wins = [];
        const { reelsCount, rowsCount } = this._config;
        const uniqueSymbols = this._getUniquePaySymbols(matrix);

        for (const sym of uniqueSymbols) {
            let ways = 1;
            let matchReels = 0;
            const wonPositions = [];

            for (let reel = 0; reel < reelsCount; reel++) {
                const matchingRows = [];
                for (let row = 0; row < rowsCount; row++) {
                    if (matrix[row][reel] === sym || this._isWild(matrix[row][reel])) {
                        matchingRows.push([reel, row]);
                    }
                }

                if (matchingRows.length === 0) break;

                ways *= matchingRows.length;
                matchReels++;
                wonPositions.push(...matchingRows);
            }

            if (matchReels >= 3) {
                const payKey = `${sym}-${matchReels}`;
                const basePay = this._paytable[payKey] || 0;

                if (basePay > 0) {
                    wins.push({
                        lineIndex: -1, // No specific line in ways
                        symbolKey: sym,
                        matchCount: matchReels,
                        wonPositions,
                        winAmount: basePay * betPerLine * ways * multiplier,
                        ways,
                    });
                }
            }
        }

        return wins;
    }

    /**
     * Cluster pay evaluation (groups of adjacent matching symbols)
     */
    _evaluateClusterWins(matrix, betPerLine, multiplier = 1) {
        const wins = [];
        const { reelsCount, rowsCount } = this._config;
        const visited = Array.from({ length: rowsCount }, () => new Array(reelsCount).fill(false));

        for (let row = 0; row < rowsCount; row++) {
            for (let reel = 0; reel < reelsCount; reel++) {
                if (visited[row][reel]) continue;

                const sym = matrix[row][reel];
                if (this._isWild(sym)) continue;

                const cluster = this._floodFill(matrix, row, reel, sym, visited);

                if (cluster.length >= 5) { // Minimum cluster size
                    const payKey = `${sym}-${Math.min(cluster.length, 15)}`; // Cap at 15+
                    const basePay = this._paytable[payKey] || 0;

                    if (basePay > 0) {
                        wins.push({
                            lineIndex: -1,
                            symbolKey: sym,
                            matchCount: cluster.length,
                            wonPositions: cluster,
                            winAmount: basePay * betPerLine * multiplier,
                        });
                    }
                }
            }
        }

        return wins;
    }

    _floodFill(matrix, startRow, startReel, targetSym, visited) {
        const { reelsCount, rowsCount } = this._config;
        const cluster = [];
        const stack = [[startRow, startReel]];

        while (stack.length > 0) {
            const [row, reel] = stack.pop();
            if (row < 0 || row >= rowsCount || reel < 0 || reel >= reelsCount) continue;
            if (visited[row][reel]) continue;
            if (matrix[row][reel] !== targetSym && !this._isWild(matrix[row][reel])) continue;

            visited[row][reel] = true;
            cluster.push([reel, row]);

            stack.push([row - 1, reel], [row + 1, reel], [row, reel - 1], [row, reel + 1]);
        }

        return cluster;
    }

    /**
     * Scatter win evaluation (position-independent)
     */
    _evaluateScatterWins(matrix, totalBet, multiplier = 1) {
        const wins = [];

        for (const rule of this._scatterRules) {
            const { symbolId, minCount, payMultipliers } = rule;
            const positions = [];

            for (let row = 0; row < matrix.length; row++) {
                for (let reel = 0; reel < matrix[row].length; reel++) {
                    if (matrix[row][reel] === symbolId) {
                        positions.push([reel, row]);
                    }
                }
            }

            if (positions.length >= minCount) {
                const payMult = payMultipliers[positions.length] || 0;
                if (payMult > 0) {
                    wins.push({
                        symbolKey: symbolId,
                        count: positions.length,
                        positions,
                        winAmount: totalBet * payMult * multiplier,
                    });
                }
            }
        }

        return wins;
    }

    /**
     * Free spin trigger evaluation
     */
    _evaluateFreeSpinTrigger(matrix) {
        const { triggerSymbol, minCount, spinsAwarded, retriggerEnabled, bonusType } = this._freeSpinRules;

        if (!triggerSymbol && triggerSymbol !== 0) {
            return { awarded: 0, retrigger: false, bonusType: null };
        }

        let count = 0;
        for (const row of matrix) {
            for (const sym of row) {
                if (sym === triggerSymbol) count++;
            }
        }

        if (count >= minCount) {
            const awarded = spinsAwarded[count] || spinsAwarded[minCount] || 0;
            return {
                awarded,
                retrigger: retriggerEnabled || false,
                bonusType: bonusType || 'freeSpins',
            };
        }

        return { awarded: 0, retrigger: false, bonusType: null };
    }

    /**
     * Jackpot evaluation (random trigger based on bet level)
     */
    _evaluateJackpot(totalBet) {
        if (!this._jackpotRules || !this._jackpotRules.tiers) return null;

        const { tiers, contributionRate = 0.01 } = this._jackpotRules;
        const contribution = totalBet * contributionRate;

        // Check each tier from highest to lowest
        for (const tier of tiers.sort((a, b) => b.threshold - a.threshold)) {
            const triggerChance = tier.probability * (totalBet / tier.qualifyingBet);
            const roll = Math.random();

            if (roll < triggerChance) {
                return {
                    tier: tier.name,
                    winAmount: tier.currentPool || tier.seedAmount,
                    contribution,
                };
            }
        }

        return { tier: null, winAmount: 0, contribution };
    }

    // =========================================================================
    // HELPER METHODS
    // =========================================================================

    _isWild(symbolId) {
        return this._wildRules && this._wildRules.symbolId === symbolId;
    }

    _calculateWildMultiplier(lineSymbols, matchCount) {
        if (!this._wildRules || !this._wildRules.multipliers) return 1;

        let mult = 1;
        for (let i = 0; i < matchCount; i++) {
            if (this._isWild(lineSymbols[i]) && this._wildRules.multipliers[i]) {
                mult *= this._wildRules.multipliers[i];
            }
        }
        return mult;
    }

    _getUniquePaySymbols(matrix) {
        const symbols = new Set();
        for (const row of matrix) {
            for (const sym of row) {
                if (!this._isWild(sym)) {
                    symbols.add(sym);
                }
            }
        }
        return symbols;
    }

    _classifyWin(totalWin, totalBet) {
        if (totalWin === 0) return 'none';
        const ratio = totalWin / totalBet;
        if (ratio < 1) return 'minor';
        if (ratio < 5) return 'standard';
        if (ratio < 15) return 'big';
        if (ratio < 50) return 'mega';
        if (ratio < 200) return 'super';
        return 'jackpot';
    }

    /**
     * Cryptographically suitable random integer
     * Uses crypto.getRandomValues when available, falls back to Math.random
     */
    _secureRandomInt(min, max) {
        if (typeof crypto !== 'undefined' && crypto.getRandomValues) {
            const range = max - min + 1;
            const bytesNeeded = Math.ceil(Math.log2(range) / 8);
            const maxValid = Math.floor((256 ** bytesNeeded) / range) * range - 1;

            let randomValue;
            do {
                const randomBytes = new Uint8Array(bytesNeeded);
                crypto.getRandomValues(randomBytes);
                randomValue = randomBytes.reduce((acc, byte, i) => acc + byte * (256 ** i), 0);
            } while (randomValue > maxValid);

            return min + (randomValue % range);
        }

        return min + Math.floor(Math.random() * (max - min + 1));
    }

    // =========================================================================
    // RTP & ANALYTICS TRACKING
    // =========================================================================

    _createSessionStats() {
        return {
            totalSpins: 0,
            totalBet: 0,
            totalWin: 0,
            bigWins: 0,
            megaWins: 0,
            superWins: 0,
            scatterHits: 0,
            freeSpinTriggers: 0,
            jackpotHits: 0,
            maxWin: 0,
            hitCount: 0,
            sessionStart: Date.now(),
        };
    }

    _trackSpin(betAmount, winAmount) {
        this._sessionStats.totalSpins++;
        this._sessionStats.totalBet += betAmount;
        this._sessionStats.totalWin += winAmount;

        if (winAmount > 0) this._sessionStats.hitCount++;
        if (winAmount > this._sessionStats.maxWin) this._sessionStats.maxWin = winAmount;

        const hitType = this._classifyWin(winAmount, betAmount);
        if (hitType === 'big') this._sessionStats.bigWins++;
        if (hitType === 'mega') this._sessionStats.megaWins++;
        if (hitType === 'super' || hitType === 'jackpot') this._sessionStats.superWins++;

        // Rolling RTP window
        this._rtpWindow.push({ bet: betAmount, win: winAmount });
        if (this._rtpWindow.length > this._rtpWindowSize) {
            this._rtpWindow.shift();
        }
    }

    /**
     * Get current session RTP
     */
    getSessionRTP() {
        if (this._sessionStats.totalBet === 0) return 0;
        return (this._sessionStats.totalWin / this._sessionStats.totalBet) * 100;
    }

    /**
     * Get rolling window RTP (more responsive than session RTP)
     */
    getRollingRTP() {
        if (this._rtpWindow.length === 0) return 0;
        const totalBet = this._rtpWindow.reduce((sum, s) => sum + s.bet, 0);
        const totalWin = this._rtpWindow.reduce((sum, s) => sum + s.win, 0);
        if (totalBet === 0) return 0;
        return (totalWin / totalBet) * 100;
    }

    /**
     * Get hit frequency (% of spins that produce any win)
     */
    getHitFrequency() {
        if (this._sessionStats.totalSpins === 0) return 0;
        return (this._sessionStats.hitCount / this._sessionStats.totalSpins) * 100;
    }

    /**
     * Get full session statistics for ARKAINBRAIN analytics pipeline
     */
    getSessionStats() {
        return {
            ...this._sessionStats,
            sessionDuration: Date.now() - this._sessionStats.sessionStart,
            sessionRTP: this.getSessionRTP(),
            rollingRTP: this.getRollingRTP(),
            hitFrequency: this.getHitFrequency(),
            theoreticalRTP: this._config.targetRTP,
            volatility: this._config.volatility,
            rtpDeviation: Math.abs(this.getSessionRTP() - this._config.targetRTP),
        };
    }

    /**
     * Reset session statistics
     */
    resetSessionStats() {
        this._sessionStats = this._createSessionStats();
        this._rtpWindow = [];
    }
}

export default MathModelController;

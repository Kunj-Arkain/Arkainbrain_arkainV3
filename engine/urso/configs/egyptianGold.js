/**
 * Example Game Configuration: "Egyptian Gold"
 * 
 * This shows how to configure a complete slot game for ARKAINBRAIN
 * integration. This config feeds the Math Model, Analytics, Free Spins,
 * and Responsible Gaming modules.
 * 
 * Usage:
 *   import ArkainBridge from './arkainBridge.js';
 *   import egyptianGoldConfig from './configs/egyptianGold.js';
 *   
 *   const bridge = new ArkainBridge({
 *       gameId: 'egyptian-gold-v1',
 *       jurisdiction: 'GB',
 *       analyticsEndpoint: 'https://api.arkainbrain.com/v1/analytics',
 *   });
 *   
 *   bridge.init(egyptianGoldConfig);
 *   
 *   // Process a spin:
 *   const result = bridge.processSpin(betPerLine, totalBet);
 *   
 *   // Get intelligence report:
 *   const report = bridge.getIntelligenceReport();
 */

const egyptianGoldConfig = {

    // =========================================================================
    // GAME IDENTITY
    // =========================================================================
    gameId: 'egyptian-gold-v1',
    gameName: 'Egyptian Gold',
    provider: 'ARKAINBRAIN',
    targetRTP: 96.50,
    volatility: 'medium-high',

    // =========================================================================
    // REEL GEOMETRY
    // =========================================================================
    reelsCount: 5,
    rowsCount: 3,
    winType: 'lines',  // 'lines' | 'ways' | 'cluster'

    // =========================================================================
    // SYMBOL DEFINITIONS
    // =========================================================================
    //
    //  0 = 10 (low)       Weight: 30
    //  1 = J  (low)       Weight: 28
    //  2 = Q  (low)       Weight: 26
    //  3 = K  (low)       Weight: 24
    //  4 = A  (low)       Weight: 22
    //  5 = Scarab (mid)   Weight: 15
    //  6 = Ankh   (mid)   Weight: 12
    //  7 = Eye    (high)  Weight: 8
    //  8 = Pharaoh(high)  Weight: 6
    //  9 = Wild           Weight: 4
    // 10 = Scatter        Weight: 3
    //
    symbolWeights: {
        0:  { weight: 30, category: 'low',     name: '10' },
        1:  { weight: 28, category: 'low',     name: 'J' },
        2:  { weight: 26, category: 'low',     name: 'Q' },
        3:  { weight: 24, category: 'low',     name: 'K' },
        4:  { weight: 22, category: 'low',     name: 'A' },
        5:  { weight: 15, category: 'mid',     name: 'Scarab' },
        6:  { weight: 12, category: 'mid',     name: 'Ankh' },
        7:  { weight: 8,  category: 'high',    name: 'Eye of Horus' },
        8:  { weight: 6,  category: 'high',    name: 'Pharaoh' },
        9:  { weight: 4,  category: 'wild',    name: 'Wild' },
        10: { weight: 3,  category: 'scatter', name: 'Scatter' },
    },

    // =========================================================================
    // WEIGHTED REEL STRIPS
    // =========================================================================
    // Each array represents the physical reel strip for that reel.
    // Symbol distribution is designed to hit target RTP of 96.50%
    //
    reelStrips: [
        // Reel 1 (40 stops)
        [0,1,2,3,4,5,0,1,2,3,6,0,1,2,4,5,0,1,3,7,0,2,3,4,5,0,1,6,0,2,3,8,0,1,4,9,0,2,10,5],
        // Reel 2 (42 stops)
        [1,0,2,4,3,6,1,0,2,3,5,1,0,4,2,7,1,0,3,5,1,2,4,6,0,1,3,8,0,2,4,5,1,0,3,9,1,2,10,0,5,3],
        // Reel 3 (44 stops)
        [2,0,1,3,5,4,2,0,1,4,6,2,0,3,1,5,2,0,4,7,2,1,3,5,0,2,4,6,1,0,3,8,2,1,0,5,2,3,9,0,1,4,10,6],
        // Reel 4 (42 stops)
        [0,1,3,2,4,5,0,1,3,4,6,0,2,1,5,3,0,1,4,7,0,2,3,6,1,0,4,5,2,3,1,8,0,2,4,9,1,3,10,0,5,2],
        // Reel 5 (40 stops)
        [1,0,2,4,3,5,1,0,3,2,6,1,0,4,5,3,1,0,2,7,1,3,4,5,0,2,6,1,3,0,8,2,4,1,9,0,3,5,10,2],
    ],

    // =========================================================================
    // PAY TABLE
    // =========================================================================
    // Format: { 'symbolId-matchCount': payout_multiplier }
    // Payout is multiplied by bet-per-line
    //
    paytable: {
        // Low symbols (10, J, Q, K, A)
        '0-3': 5,    '0-4': 25,   '0-5': 100,
        '1-3': 5,    '1-4': 25,   '1-5': 100,
        '2-3': 5,    '2-4': 30,   '2-5': 125,
        '3-3': 5,    '3-4': 30,   '3-5': 125,
        '4-3': 10,   '4-4': 40,   '4-5': 150,

        // Mid symbols (Scarab, Ankh)
        '5-3': 15,   '5-4': 75,   '5-5': 250,
        '6-3': 20,   '6-4': 100,  '6-5': 400,

        // High symbols (Eye, Pharaoh)
        '7-3': 30,   '7-4': 150,  '7-5': 750,
        '8-3': 50,   '8-4': 250,  '8-5': 1000,

        // Wild (pays as highest symbol)
        '9-3': 50,   '9-4': 250,  '9-5': 2500,
    },

    // =========================================================================
    // PAYLINES (20 lines, standard 5×3 layout)
    // =========================================================================
    // Each line is an array of row indices [reel0, reel1, reel2, reel3, reel4]
    //
    paylines: [
        [1, 1, 1, 1, 1],  // Line 0: Middle row
        [0, 0, 0, 0, 0],  // Line 1: Top row
        [2, 2, 2, 2, 2],  // Line 2: Bottom row
        [0, 1, 2, 1, 0],  // Line 3: V shape
        [2, 1, 0, 1, 2],  // Line 4: Inverted V
        [0, 0, 1, 2, 2],  // Line 5: Down slope
        [2, 2, 1, 0, 0],  // Line 6: Up slope
        [1, 0, 0, 0, 1],  // Line 7: Top dip
        [1, 2, 2, 2, 1],  // Line 8: Bottom dip
        [0, 1, 0, 1, 0],  // Line 9: Zigzag top
        [2, 1, 2, 1, 2],  // Line 10: Zigzag bottom
        [1, 0, 1, 0, 1],  // Line 11: Zigzag mid-top
        [1, 2, 1, 2, 1],  // Line 12: Zigzag mid-bot
        [0, 1, 1, 1, 0],  // Line 13: Shallow V
        [2, 1, 1, 1, 2],  // Line 14: Shallow inv V
        [0, 0, 1, 0, 0],  // Line 15: Top bump
        [2, 2, 1, 2, 2],  // Line 16: Bottom bump
        [1, 0, 1, 2, 1],  // Line 17: Wave right
        [1, 2, 1, 0, 1],  // Line 18: Wave left
        [0, 2, 0, 2, 0],  // Line 19: Deep zigzag
    ],

    // =========================================================================
    // WILD RULES
    // =========================================================================
    wildRules: {
        symbolId: 9,
        substitutesFor: [0, 1, 2, 3, 4, 5, 6, 7, 8], // Everything except scatter
        appearsOnReels: [1, 2, 3],                      // Reels 2, 3, 4 only
        multipliers: null,                               // No wild multipliers
        expanding: false,
        sticky: false,
    },

    // =========================================================================
    // SCATTER RULES
    // =========================================================================
    scatterRules: [
        {
            symbolId: 10,
            minCount: 3,
            payMultipliers: {
                3: 2,    // 3 scatters = 2× total bet
                4: 10,   // 4 scatters = 10× total bet
                5: 50,   // 5 scatters = 50× total bet
            },
        },
    ],

    // =========================================================================
    // FREE SPIN RULES
    // =========================================================================
    freeSpinRules: {
        triggerSymbolId: 10,   // Scatter triggers free spins
        minTriggerCount: 3,

        spinsAwarded: {
            3: 10,    // 3 scatters = 10 free spins
            4: 15,    // 4 scatters = 15 free spins
            5: 25,    // 5 scatters = 25 free spins
        },

        retriggerEnabled: true,
        retriggerMinCount: 3,
        retriggerSpins: {
            3: 5,
            4: 10,
            5: 15,
        },
        maxRetriggers: 3,

        multiplierMode: 'escalating',
        baseMultiplier: 1,
        escalationStep: 1,    // +1× every spin (1×, 2×, 3×, ...)
        maxMultiplier: 10,

        introAnimationDuration: 2500,
        outroAnimationDuration: 2000,
        spinDelay: 400,
    },

    // =========================================================================
    // JACKPOT RULES (4-tier progressive)
    // =========================================================================
    jackpotRules: {
        contributionRate: 0.012, // 1.2% of each bet feeds jackpot pool
        tiers: [
            {
                name: 'Grand',
                seedAmount: 100000,
                currentPool: 100000,
                probability: 0.000001,   // ~1 in 1,000,000 spins
                qualifyingBet: 5.00,
            },
            {
                name: 'Major',
                seedAmount: 10000,
                currentPool: 10000,
                probability: 0.00001,    // ~1 in 100,000 spins
                qualifyingBet: 2.00,
            },
            {
                name: 'Minor',
                seedAmount: 1000,
                currentPool: 1000,
                probability: 0.0001,     // ~1 in 10,000 spins
                qualifyingBet: 1.00,
            },
            {
                name: 'Mini',
                seedAmount: 100,
                currentPool: 100,
                probability: 0.001,      // ~1 in 1,000 spins
                qualifyingBet: 0.20,
            },
        ],
    },

    // =========================================================================
    // BET CONFIGURATION
    // =========================================================================
    betConfig: {
        defaultBet: 1,
        bets: [1, 2, 5, 10, 20, 50],
        defaultCoin: 0.01,
        coinValues: [0.01, 0.02, 0.05, 0.10, 0.20, 0.50, 1.00],
        defaultLines: 20,
    },
};

export default egyptianGoldConfig;

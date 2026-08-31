inlets = 2;
outlets = 1;

// ------------------------------------
// 設定
// ------------------------------------

// 位置追従の滑らかさ
var smoothing = 0.12;

// target付近をどれくらいウロウロするか
// spread=0 のときでもこの範囲で動く
var wanderRange = 12.0; // ±12度

// ランダム目標を変更する間隔
var wanderInterval = 250; // ms

// これ以上角度入力が途絶えたら停止
var inputTimeout = 200; // ms

// 0 = 集合
// 1 = 大きく散開
var spread = 0.0;

// 最大散開範囲
var maxSpread = 140.0;


// ------------------------------------
// 状態
// ------------------------------------

var center = 0.0;

var current = [];
var wander = [];
var wanderTarget = [];

var initialized = false;

var lastInputTime = 0;
var lastWanderTime = 0;

for (var i = 0; i < 8; i++) {
    current[i] = 0;
    wander[i] = 0;
    wanderTarget[i] = 0;
}


// ------------------------------------
// utilities
// ------------------------------------

function now()
{
    return new Date().getTime();
}


function angleDelta(from, to)
{
    var d = to - from;

    while (d > 180)
        d -= 360;

    while (d < -180)
        d += 360;

    return d;
}


function randomRange(min, max)
{
    return min + Math.random() * (max - min);
}


// ------------------------------------
// input
// ------------------------------------

function msg_float(v)
{
    // 左 inlet = 手の角度
    if (inlet === 0) {

        center = v;
        lastInputTime = now();

        if (!initialized) {

            for (var i = 0; i < 8; i++) {
                current[i] = center;
                wander[i] = randomRange(-wanderRange, wanderRange);
                wanderTarget[i] = wander[i];
            }

            initialized = true;
        }

        startTask();
    }

    // 右 inlet = spread 0〜1
    else if (inlet === 1) {

        spread = Math.max(0, Math.min(1, v));
    }
}


// ------------------------------------
// movement
// ------------------------------------

function tick()
{
    var t = now();

    // 角度入力が止まったらOSCも停止
    if (t - lastInputTime > inputTimeout) {
        task.cancel();
        return;
    }


    // 一定時間ごとに新しいwander目標を作る
    if (t - lastWanderTime > wanderInterval) {

        for (var i = 0; i < 8; i++) {

            // spreadが大きいほど漂う範囲も拡大
            var range =
                wanderRange +
                spread * maxSpread;

            wanderTarget[i] =
                randomRange(-range, range);
        }

        lastWanderTime = t;
    }


    for (var i = 0; i < 8; i++) {

        // wander自体もゆっくり追従
        wander[i] +=
            (wanderTarget[i] - wander[i]) * 0.04;


        // center + 独立したwander
        var desired =
            center + wander[i];


        // 0/360をまたいでも最短方向
        var diff =
            angleDelta(current[i], desired);

        current[i] += diff * smoothing;


        var source = i + 2;

        outlet(
            0,
            "/source/" + source + "/aed",
            current[i],
            0,
            1
        );
    }
}


function startTask()
{
    if (!task.running) {
        task.interval = 20;
        task.repeat();
    }
}


var task = new Task(tick, this);
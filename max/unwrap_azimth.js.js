inlets = 1;
outlets = 1;

// 前回の生azimuth
var prevRaw = null;

// 連続化した角度
var unwrapped = 0;


// float入力
function msg_float(v) {
    process(v);
}

// int入力
function msg_int(v) {
    process(v);
}

function process(v) {
    // 最初の1回はそのまま初期値にする
    if (prevRaw === null) {
        prevRaw = v;
        unwrapped = v;
        outlet(0, unwrapped);
        return;
    }

    // 今回 - 前回
    var delta = v - prevRaw;

    // 0/360またぎ補正
    if (delta > 180) {
        delta -= 360;
    } else if (delta < -180) {
        delta += 360;
    }

    // 累積して連続角度化
    unwrapped += delta;

    // 前回値更新
    prevRaw = v;

    // 出力
    outlet(0, unwrapped);
}


// 状態リセット
function reset() {
    prevRaw = null;
    unwrapped = 0;
}


// 必要なら現在値を強制セット
function set(v) {
    prevRaw = v;
    unwrapped = v;
    outlet(0, unwrapped);
}
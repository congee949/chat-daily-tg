#!/bin/bash
# power_aware_disablesleep.sh — 插电禁睡 / 拔电恢复正常睡眠。
#
# 背景:MacBook 合盖(clamshell)会强制睡眠,launchd 定时任务(频道转发器)因此
# 被跳过,资讯积压到下次唤醒才批量推送。pmset 的 disablesleep 能阻止合盖睡眠,
# 但它是全局开关(-c 不分档,电池也会被一起禁睡 → 带出门合盖放包会过热)。
# 本脚本按当前电源动态开关它:插电 disablesleep=1(合盖不睡、8 个调度点全覆盖),
# 拔电=0(恢复正常睡眠,带走不过热、不空耗电)。
#
# 由 com.chat-daily-tg.disablesleep LaunchDaemon 以 root 每 60s 调用一次;只在
# 目标值与当前值不同时才写,避免每分钟无谓调用 pmset。
set -u

if pmset -g batt | grep -q "'AC Power'"; then
  want=1
else
  want=0
fi

have=$(pmset -g | awk '/SleepDisabled/{print $2; exit}')
have=${have:-0}

if [ "$want" != "$have" ]; then
  pmset disablesleep "$want"
  logger -t cd-disablesleep "power source changed -> disablesleep=$want"
fi

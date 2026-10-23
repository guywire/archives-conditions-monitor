// 15-minute means/extremes into environment_rollup (kept indefinitely; raw data is kept 2 years)
option task = {name: "rollup_15m", every: 15m, offset: 2m}

data = from(bucket: "environment_raw")
  |> range(start: -task.every)
  |> filter(fn: (r) => r._measurement == "environment" or r._measurement == "air_quality" or r._measurement == "leak")

data
  |> filter(fn: (r) => r._field == "temperature_c" or r._field == "humidity_rh" or r._field == "dew_point_c"
      or r._field == "pm25" or r._field == "pm10" or r._field == "co2_ppm" or r._field == "voc_index" or r._field == "nox_index")
  |> aggregateWindow(every: 15m, fn: mean, createEmpty: false)
  |> to(bucket: "environment_rollup")

data
  |> filter(fn: (r) => r._field == "status" or r._field == "water")
  |> toFloat()
  |> aggregateWindow(every: 15m, fn: max, createEmpty: false)
  |> to(bucket: "environment_rollup")

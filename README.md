# Bot de "mechazos" USDT/ARS en Bitso — spec maestra + implementación

Este documento es el **prompt maestro** de la estrategia: la versión detallada y
formalizada de la idea original ("detectar mechazos grandes a ciertos horarios,
comprar en el piso, vender arriba del 2-3%, de forma conservadora"). Todo el
código en `src/` y `scripts/` implementa exactamente lo que está descripto acá.
Si en algún momento el código y este documento no coinciden, este documento
manda: ajustá el código, no al revés.

## 0. Lo que esto NO es

- **No es una promesa de "nunca perder plata".** Ninguna estrategia puede
  garantizar eso: el mercado puede caer y no revertir nunca, o revertir
  después de que canceles por timeout. Lo que sí hace este diseño es ser
  **conservador a propósito**: posiciones chicas, una sola a la vez, siempre
  con límite de pérdida máxima por operación y por día, y con un "kill switch"
  que apaga el bot solo si las cosas salen mal.
- **No es asesoramiento financiero.** Es una herramienta técnica que vos
  configurás, revisás y corrés bajo tu propio criterio y riesgo.
- **No usa apalancamiento ni margen**, aunque el banner de "Bitso Alpha" que
  viste en la web sea justamente margen (10x). Este bot opera siempre en spot,
  con capital propio, sin pedir prestado.

## 1. La idea, formalizada

1. **Mercado**: `usdt_ars` en Bitso (USDT/ARS spot).
2. **Señal de entrada**: se calcula un **precio de referencia** (VWAP —precio
   promedio ponderado por volumen— de los últimos N minutos, configurable,
   default 30). Si el precio cae **2% o más** (configurable) por debajo de esa
   referencia, se considera un "mechazo" comprable.
3. **Entrada**: se coloca una **orden límite de compra** apenas por encima del
   mínimo detectado (+0.1% configurable, para tener prioridad de ejecución sin
   pagar de más). Nunca se persigue el precio subiendo la oferta: si no se
   llena en `buy_order_timeout_seconds` (default 90s), se cancela y se espera
   la próxima señal.
4. **Salida normal**: apenas se llena la compra, se coloca automáticamente una
   **orden límite de venta** en `entry_price * (1 + take_profit_pct)` (default
   2.5%, configurable entre 2-3% como pediste).
5. **Red de seguridad (stop duro)**: si el precio sigue cayendo hasta
   `hard_stop_loss_pct` por debajo de la entrada (default 4%), se cancela la
   venta límite y se vende **a mercado** para cortar la pérdida. Es la única
   excepción a "solo órdenes límite", y existe porque prometer cero pérdidas
   sería mentirte: sin este stop, una caída sin reversión te dejaría
   sosteniendo una posición perdedora indefinidamente.
6. **Filtro horario**: opcional. Se completa corriendo
   `scripts/analyze_patterns.py` sobre datos históricos propios (ver sección 3).
   Mientras no haya suficiente evidencia estadística, el filtro queda vacío
   (opera en todas las horas) en vez de inventar una ventana horaria al voleo.

## 2. Gestión de riesgo (lo que lo hace "conservador")

Todo vive en `config.yaml`, nada hardcodeado:

| Control | Default | Qué hace |
|---|---|---|
| `capital.position_size_ars` | 75.000 ARS | Tamaño fijo por operación (pediste 50-100k para arrancar) |
| `capital.max_concurrent_positions` | 1 | Nunca dos operaciones abiertas a la vez |
| `capital.max_daily_trades` | 6 | Tope duro de operaciones por día |
| `capital.max_daily_loss_pct` | 1.5% | Si las pérdidas realizadas del día superan esto, el bot **se apaga solo** (kill switch) hasta el otro día / reinicio manual |
| `strategy.cooldown_minutes_after_trade` | 15 min | Pausa obligatoria después de cada operación (gane o pierda) |
| `strategy.hard_stop_loss_pct` | 4% | Red de seguridad descripta arriba |
| `strategy.min_net_profit_after_fees_pct` | 1.0% | El take-profit configurado tiene que superar comisión ida+vuelta + este margen, o el bot rechaza arrancar (ver `risk_manager.validate_take_profit`) |

Con las comisiones default de `usdt_ars` (0.45% maker / 0.60% taker, ver
`config.yaml > fees`, **verificalas en tu cuenta real** porque dependen de tu
volumen de 30 días), un take-profit de 2.5% deja aproximadamente **1.6% neto**
después de pagar 0.45% de ida + 0.45% de vuelta (ambas operaciones son límite,
así que en el camino normal pagás tarifa de *maker*, no de *taker*).

## 3. Por qué hay que recolectar datos antes de confiar en el filtro horario

Bitso **no tiene un endpoint público de velas históricas** (confirmado contra
su documentación oficial): solo expone trades recientes, ticker de 24hs y el
libro de órdenes actual. Esto significa que la única forma honesta de saber
"a qué horas pasan más mechazos" es **grabar el mercado en vivo** con
`scripts/collect_data.py` y dejarlo corriendo un tiempo (idealmente varias
semanas, cubriendo distintos días de la semana) antes de sacar conclusiones.

El flujo de trabajo completo es:

```
1. python scripts/collect_data.py        # dejar corriendo en background, días/semanas
2. python scripts/analyze_patterns.py    # cuando haya suficiente historial
3. python scripts/run_backtest.py        # validar la estrategia completa con fees reales
4. python scripts/run_bot.py             # dry-run primero (default)
5. python scripts/run_bot.py --live      # solo cuando el dry-run te convenza (ver seguridad abajo)
```

## 4. Arquitectura del código

```
config.yaml           Toda la configuración (capital, riesgo, estrategia, fees)
.env / .env.example    Credenciales (NUNCA se commitean; .env está en .gitignore)

src/
  bitso_client.py      Cliente REST firmado (HMAC-SHA256, nonce v2) + endpoints públicos
  data_store.py        SQLite: trades, velas, eventos de mechazo, log de operaciones
  candle_builder.py    Agrega trades en velas OHLC propias (no hay API de velas)
  pattern_analyzer.py  Detecta mechazos históricos y los resume por hora del día
  strategy.py          Lógica PURA de decisión (sin I/O): señal de compra, TP, stop duro
  risk_manager.py       Límites de capital, kill switch diario, cooldown, tamaño de posición
  backtester.py        Simulación trade-a-trade sobre datos históricos reales
  live_engine.py        Motor en vivo: conecta strategy+risk con Bitso real o simulado

scripts/
  collect_data.py       Recolector continuo de trades públicos
  analyze_patterns.py   Reporte de patrones por hora
  run_backtest.py       Corre el backtest con la config actual
  run_bot.py             Entry point del bot (dry-run por default)

tests/                  15 tests unitarios (estrategia, riesgo, velas, backtest)
```

`strategy.py` es compartido textualmente entre el backtester y el motor en
vivo: la misma función que decide "¿hay señal de compra?" en el backtest es la
que decide en producción, para que el resultado del backtest sea representativo
de lo que el bot realmente va a hacer.

## 5. Seguridad: cómo está armado para que NO salga una orden real por accidente

Tres trabas independientes, **las tres tienen que estar puestas** para que el
bot opere con dinero real:

1. `config.yaml > mode.dry_run` tiene que ser `false`.
2. `.env > BITSO_LIVE_CONFIRM` tiene que valer exactamente `YES`.
3. Correr `run_bot.py` con el flag explícito `--live`.

Si falta cualquiera de las tres, el bot opera en modo simulado: lee datos
públicos reales del mercado, "coloca" y "llena" órdenes en memoria/SQLite, pero
**nunca llama a un endpoint privado de Bitso**. Así podés ver cómo se comporta
la estrategia en tiempo real, con precios reales, sin arriesgar un peso.

Además:
- Las credenciales (`BITSO_API_KEY` / `BITSO_API_SECRET`) solo se leen de tu
  `.env` local — nunca se piden ni se pegan en un chat.
- Generá la API key en Bitso con permiso de **Trading** únicamente. No le des
  permiso de retiros/withdrawals: el bot no los necesita y así, aunque la key
  se filtrara, no se puede sacar fondos de la cuenta con ella.

## 6. Qué falta validar antes de poner plata real (en orden)

1. Dejar `collect_data.py` corriendo un tiempo razonable (empezá a mirar
   resultados preliminares a la semana, pero cuantas más semanas mejor).
2. Correr `analyze_patterns.py`: ¿hay suficientes mechazos de ≥2% como para
   que valga la pena? ¿En qué horas se concentran? ¿Revierten con buena tasa
   hacia el take-profit o solo "casi llegan"?
3. Correr `run_backtest.py` con y sin filtro horario, variando
   `wick_drop_threshold_pct` y `take_profit_pct` en `config.yaml`. Buscá un
   resultado **consistentemente positivo**, no el mejor número que encontraste
   probando combinaciones (eso es sobreajuste, no una estrategia real).
4. Correr `run_bot.py` en dry-run por un tiempo, en paralelo a la vida real del
   mercado, y comparar lo que el bot "hubiera hecho" contra lo que vos
   observás a ojo en el gráfico.
5. Recién ahí, con un monto chico (los 50-100k ARS de prueba que mencionaste),
   pasar a modo real.

## 7. Limitaciones honestas del backtest (leer antes de confiar en los números)

- No modela profundidad real del libro: asume que tu orden límite se llena
  completa apenas el precio toca tu nivel, sin competencia de otras órdenes
  delante tuyo en la misma cola de precio.
- No modela latencia de red, caídas momentáneas de conexión, ni rate limits.
- Los datos históricos son los que vos mismo grabaste con `collect_data.py`:
  el backtest nunca es "más largo" que el tiempo que lo dejaste corriendo.
- Que un patrón se haya repetido en el pasado no garantiza que se repita en el
  futuro.

## 8. Cómo arrancar (resumen operativo)

```powershell
cd "D:\BOOT ARBITRAJE"
python -m pip install -r requirements.txt
copy .env.example .env
# Editá .env y pegá ahí tu BITSO_API_KEY / BITSO_API_SECRET reales (con permiso de Trading).

python scripts/collect_data.py
# ... dejarlo corriendo. Más adelante, en otra terminal:

python scripts/analyze_patterns.py
python scripts/run_backtest.py
python scripts/run_bot.py
```

El repo ya corrió exitosamente: 15/15 tests unitarios pasando, y un smoke test
en vivo contra datos públicos reales de `usdt_ars` sin errores.

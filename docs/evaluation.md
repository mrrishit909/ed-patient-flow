# Evaluation

Everything here is measured on synthetic departments generated with seeds the demo does not use (`(101, 102, 103)`). Reproduce with
`python -m ed.evaluate` (about two minutes). The numbers say how well the methods do against the department as the simulator plays it.
The forward simulation is the generator itself run from the live state with fresh randomness, so its errors are the floor a real twin
could reach, not what one would reach: on a real department the twin is an approximation and the bands would be wider.

## 1. Models fitted on sixteen weeks, tested on the last two

| department (seed) | arrivals/h error | same hour last week | inside 80% band | admission AUC | triage level alone | ED stay error (min) | median by triage |
|---|---|---|---|---|---|---|---|
| 101 | 1.83 | 2.56 | 87% | 0.792 | 0.74 | 92.2 | 93.2 |
| 102 | 1.93 | 2.56 | 85% | 0.78 | 0.724 | 92.1 | 92.9 |
| 103 | 1.88 | 2.63 | 88% | 0.789 | 0.751 | 65.7 | 64.8 |

The arrival profile beats the same-hour-last-week baseline by about 27% and its Poisson band is about right. The admission model adds a
little over triage level alone (age, complaint and hour carry some signal). The length-of-stay model is no better than the median by
triage level, which the page says: most of the variation in this world is noise around a triage-level mean, and the model cannot see
imaging or the nurse ratio at arrival.

## 2. The forward simulation, scored on 6 mornings

At 07:00 each day the department is run forward 12 hours 20 times and the P50 compared with what then happened. "Yesterday" is the
same window the day before, which is what a charge nurse has without a simulation.

| department (seed) | mornings | peak waiting error | yesterday's peak | truth under P90 | door-to-bed P90 error (min) | truth inside P10–P90 |
|---|---|---|---|---|---|---|
| 101 | 6 | 8.8 | 10.0 | 83% | 34.4 | 67% |
| 102 | 6 | 28.9 | 25.7 | 67% | 32.0 | 67% |
| 103 | 6 | 6.2 | 6.3 | 67% | 33.9 | 67% |

On the point estimate the simulation is no better than yesterday's peak, and the page should not claim otherwise: the morning
state carries little information about the afternoon, and both the simulation and yesterday are guesses around the same profile. The
truth falls under the simulation's P90 72% of the time, so the band is a little narrow for a P90; what the simulation adds is a band at all,
and a reaction to the live state (how full the beds already are, who is boarding) that yesterday cannot give.

## 3. The surge, with and without action

The demo's disruption on each department (30 injured patients over an hour, ward discharges at 40% for eight hours), and the packages of
actions, over 12 hours and 20 runs. Peak waiting room, median door-to-bed and peak boarding, P50 of the runs.

| department (seed) | peak waiting | with the package | 6 beds only | 4 nurses only | door-to-bed (min) | with the package | peak boarding | with the package |
|---|---|---|---|---|---|---|---|---|
| 101 | 45.0 | 33.0 | 33.5 | 45.0 | 17.0 | 10.5 | 0.0 | 0.0 |
| 102 | 41.5 | 27.5 | 30.0 | 41.5 | 8.8 | 6.2 | 1.5 | 3.0 |
| 103 | 43.0 | 33.0 | 32.5 | 43.0 | 15.4 | 9.8 | 0.0 | 0.0 |

The package (six surge beds, two nurses on two shifts, a discharge push) cuts peak waiting by about 28% and halves the median wait,
and the waiting room is still far from clear after twelve hours. Beds alone do most of it; nurses alone do little, because the ratio
only bites when the ED is far over its beds. Boarding stays low here because the ED itself is the bottleneck: it admits patients no faster
than it can treat them, so the wards' slowdown is felt as a few boarders late in the day rather than a crisis. That is a property of this
department's numbers, and a department whose wards run fuller would show the opposite.

## 4. What is not measured

- The staffing optimiser is a small exact solve; its value is the simulation's, which is measured above.
- Nothing clinical: the simulator has no diagnoses, treatments or outcomes, only times, levels and dispositions.
- Real EHR feeds, HL7v2, and a department whose rules differ from this generator's.

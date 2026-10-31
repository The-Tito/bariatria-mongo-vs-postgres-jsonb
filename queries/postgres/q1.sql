-- Q1 (P15): medicación crónica por año y fármaco. Equivale a queries/mongo/q1.json.
-- El año usa el desfase fijo de la Ciudad de México (UTC-6), igual que $year con timezone "-06:00".
-- El nombre usa COLLATE "C": se agrupa y ordena por bytes, como MongoDB compara las cadenas.
SELECT EXTRACT(YEAR FROM (data ->> 'fecha')::timestamptz AT TIME ZONE INTERVAL '-06:00')::int AS anio,
       (med ->> 'nombre') COLLATE "C" AS medicamento,
       COUNT(*) AS prescripciones,
       SUM((med ->> 'dosis_mg')::float8 * (med ->> 'tomas_por_dia')::int) AS dosis_diaria_total_mg,
       AVG((med ->> 'dosis_mg')::float8 * (med ->> 'tomas_por_dia')::int) AS dosis_diaria_prom_mg
FROM visitas, jsonb_array_elements(data -> 'medicamentos') AS med
WHERE (med ->> 'duracion_dias')::int >= 60
GROUP BY 1, 2
ORDER BY anio, medicamento;

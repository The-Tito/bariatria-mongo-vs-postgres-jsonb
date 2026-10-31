-- Q2 (P14): auditoría de captura, visitas cerradas de 2026 (hora local) con el IMC más alto.
-- Equivale a queries/mongo/q2.json. La fecha se compara como texto con COLLATE "C" para usar el índice;
-- NULLS LAST reproduce que MongoDB deja los faltantes al final, y el id desempata como el _id.
SELECT id,
       data ->> 'paciente_id' AS paciente_id,
       data ->> 'fecha' AS fecha,
       (data -> 'resumen' ->> 'imc')::float8 AS imc,
       (data -> 'resumen' ->> 'peso_kg')::float8 AS peso_kg
FROM visitas
WHERE data ->> 'estado' = 'cerrada'
  AND (data ->> 'fecha') COLLATE "C" >= '2026-01-01T06:00:00.000Z'
  AND (data ->> 'fecha') COLLATE "C" <  '2027-01-01T06:00:00.000Z'
ORDER BY (data -> 'resumen' ->> 'imc')::float8 DESC NULLS LAST, id
LIMIT 1000;

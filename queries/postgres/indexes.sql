-- Se ejecuta después de la carga, igual que en MongoDB.

-- Índice de fecha (equivalente a { fecha: 1 } en MongoDB). La fecha se guarda como texto ISO 8601 en
-- UTC de ancho fijo: con COLLATE "C" se compara byte a byte, que es el orden cronológico, y la expresión
-- es inmutable. Un cast a timestamptz no es inmutable y Postgres no permite indexarlo.
CREATE INDEX IF NOT EXISTS idx_visitas_fecha ON visitas (((data ->> 'fecha') COLLATE "C"));

-- Estadísticas para el planificador (sin ellas no estima el índice de expresión) y mapa de visibilidad.
VACUUM (ANALYZE) visitas;

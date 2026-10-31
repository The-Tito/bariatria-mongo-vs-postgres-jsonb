-- Clon JSONB de la colección visitas: el mismo documento de MongoDB en una sola columna.
-- No es un modelo relacional; las conversiones de tipos BSON a JSON están en queries/README.md.
CREATE TABLE IF NOT EXISTS visitas (
    id   uuid  PRIMARY KEY,
    data jsonb NOT NULL
);

cd /home3/haons/Proj-SemWeb/Book-LOD-App/sparql-endpoint/apache-jena-fuseki-6.2.0

abs_ontology_ttl="/home3/haons/Proj-SemWeb/Book-LOD-App/ontology.ttl"
abs_data_ttl="/home3/haons/Proj-SemWeb/Book-LOD-App/linking/output/full/books_5star.ttl" # "/home3/haons/Proj-SemWeb/Book-LOD-App/pipeline/output/books_data.ttl"

./fuseki-server \
    --file="${abs_ontology_ttl}" \
    --file="${abs_data_ttl}" \
    /ds

# Ctrl+shift+P -> Forward a port -> Enter "3030" -> access link: http://localhost:3030

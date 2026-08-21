---
repo: architecture
path: docs/architecture/aw-app-blender.md
source: generated
edited: false
checksum: sha256:faa501cda01bf980616a3e1f4ce2f0cb826691ac8c951f9254d5a7fd36d4c4b1
---
# Blender

- **repo**: aw-app-blender
- **layer**: app-container
- **technologies**: docker
- **health** (derived): planned

Blender 3D in the browser (linuxserver/blender over KasmVNC), with a persistent /config so scenes, preferences and add-ons survive container recreates — plus the aw-blender MCP, which drives the running Blender from an agent session (scene inspection, arbitrary bpy execution, viewport screenshots, Poly Haven / Sketchfab / Hyper3D / Hunyuan3D asset providers). Ported as-is from the agentic-workspace monolith's aw-custom-blender docker service + aw-blender MCP.

## Connections
_none_

## MCP tools
_none exposed_

## Requirements
### O screenshot volta como caminho, e a imagem inline é opt-in
- Given o processo MCP roda no container do gateway e não compartilha filesystem com o Blender, mas o volume $AW_APP_DATA vive na árvore do workspace, que toda sessão de agente lê
- When a captura é feita e o retorno é montado (repos/aw-app-blender/mcp_server/server.py::_screenshot:481)
- Then o padrão devolve o caminho no workspace e o caminho de dentro do Blender, em texto, e só com inline=true a imagem volta em base64 pelo canal de comando — imagem inline custa tokens de contexto em TODA chamada, inclusive nas muitas em que quem chamou só queria o arquivo. O upstream escreve em tempfile.gettempdir() e abre localmente, o que aqui daria um caminho para nada, já que os dois lados não veem o mesmo disco
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-blender/tests/test_mcp_server.py` (passing)

### Screenshot que não chegou a existir vira erro, não um caminho pendurado
- Given uma sessão sem viewport 3D aberto, situação em que o add-on relata sucesso mesmo sem ter escrito nada
- When o tamanho do arquivo é sondado de dentro do Blender depois da captura (repos/aw-app-blender/mcp_server/server.py::_screenshot:512-529)
- Then tamanho zero ou ilegível vira BlenderError dizendo que o add-on precisa de sessão GUI com viewport aberto, em vez de devolver um caminho bonito para um arquivo inexistente — confiar no "ok" do add-on é o erro clássico aqui, porque o sintoma aparece muito depois, quando alguém tenta abrir o PNG. O ValueError do int() é tratado como zero de propósito (linha 523): saída não numérica é tão inconclusiva quanto ausência
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-blender/tests/test_mcp_server.py` (passing)

### Nome de arquivo vindo do chamador não escapa do diretório de saída
- Given o nome do screenshot é escolhido por quem chama a tool, e o caminho final é interpolado num código Python executado dentro do Blender
- When o nome é reduzido a um único segmento (repos/aw-app-blender/mcp_server/server.py::_safe_name:459)
- Then só o basename sobrevive, "." e ".." viram None e caem no nome gerado, e a extensão .png é forçada — sem isso um nome como ../../config/autostart.sh escreveria fora do diretório de saída, dentro de um volume que persiste entre recriações do container. Cada captura também incrementa um contador e cai em viewport-&lt;pid&gt;-&lt;n&gt;.png quando não vem nome (server.py:501-503), então duas capturas seguidas não se sobrescrevem
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-blender/tests/test_mcp_server.py` (passing)

### Erro do add-on e falha de conexão viram resultado de erro, nunca exceção que derruba a sessão
- Given um servidor MCP stdio cuja morte leva junto a sessão inteira do cliente, e um Blender que pode estar sem o add-on instalado ou fora do ar
- When uma tool desconhecida é chamada, o add-on devolve erro, ou a conexão falha (repos/aw-app-blender/mcp_server/server.py::call_tool:555 e get_connection:190)
- Then tudo volta como resultado de erro via _text(..., is_error=True) (server.py:208), e a falha de conexão carrega a dica de instalar o add-on — o agente lê o problema e corrige, em vez de perder a sessão. É o que torna a instalação manual do BlenderMCP um passo diagnosticável: sem a dica o sintoma é uma tool que simplesmente não responde
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-blender/tests/test_mcp_server.py` (passing)

### Toda tool anunciada tem schema e é despachável, e as integrações opcionais recusam cedo
- Given as vinte e duas tools que este servidor anuncia, entre elas famílias que dependem de integrações que podem estar desligadas (Poly Haven, Sketchfab, Hyper3D, Hunyuan3D)
- When o inventário anunciado é cruzado com o dispatch (repos/aw-app-blender/tests/test_mcp_server.py::test_every_advertised_tool_has_a_schema_and_is_dispatchable:88) e as guardas de integração rodam (repos/aw-app-blender/mcp_server/server.py::_require_polyhaven:452)
- Then nenhuma tool aparece no tools/list sem inputSchema ou sem destino em call_tool, e uma família desligada recusa com a mensagem que o próprio Blender deu, antes de tentar trabalho — anunciar uma tool que não despacha é pior que não anunciar: o agente escolhe ela, chama, e recebe o silêncio de um else que não existe
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-blender/tests/test_mcp_server.py` (passing)

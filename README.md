# QTC Referências externas

Gestor de vínculos para ficheiros IngeTrazo (`.igz`). Permite registar, localizar, carregar, descarregar, sincronizar e remover referências externas associadas ao projeto aberto.

> Estado: o plugin gere vínculos e importa a geometria de um IGZ como componente do projeto central. Não é ainda um XREF nativo do núcleo do IngeTrazo; consulte [Limitações](#limitações-do-núcleo-e-da-api).

## Instalação

1. Copie a pasta `QTC_Referencias_externas` completa para a pasta de plugins do IngeTrazo.
2. No Windows, a localização habitual é `%APPDATA%\\IngeTrazo\\plugins\\`.
3. Confirme que não existe uma cópia antiga de `igz_xref_manager` na pasta de plugins, para evitar o carregamento duplicado.
4. Reinicie o IngeTrazo.
5. Abra **Extensões → QTC Referências externas**.

## Estrutura do plugin

```text
QTC_Referencias_externas/
├── __init__.py
├── QTC_Referencias_externas.py
├── README.md
├── dialogs/
│   └── xref_progress.py
└── Icones/
    ├── add_reference.svg
    ├── export_project.svg
    ├── export_compact.svg
    ├── file_igz.svg
    ├── load_objects.svg
    ├── open_project.svg
    ├── unload_objects.svg
    ├── relocate_file.svg
    ├── remove_reference.svg
    ├── update_all.svg
    ├── update_state.svg
    ├── verify_paths.svg
    └── status_*.svg
```

- `__init__.py` é apenas a ponte de descoberta exigida pelo IngeTrazo. Reexporta a configuração e a ferramenta do módulo principal.
- `QTC_Referencias_externas.py` contém a interface, a gestão dos vínculos, a importação/remoção de componentes e os controlos de atualização.
- `dialogs/xref_progress.py` apresenta o progresso das operações que modificam vínculos ou os respetivos componentes.
- `Icones/` contém exclusivamente os ícones SVG usados pela interface e pelos menus.

## Dados guardados no projeto

Os vínculos são guardados nos metadados do documento atual, na chave `references`. Cada registo contém um identificador, caminho absoluto do IGZ, descrição, marca temporal do ficheiro de origem, data da última operação e, quando carregado, o identificador do componente criado no modelo central.

Os vínculos pertencem somente ao projeto onde foram criados. Vínculos existentes dentro de um IGZ referenciado não são propagados para o projeto central.

## Formato IGP — entrega do projeto completo

O ficheiro `.igp` é **exclusivamente um formato de transmissão/entrega do projeto completo num único ficheiro comprimido**. Inclui uma cópia do projeto principal, os ficheiros IGZ dos vínculos disponíveis e um manifesto que permite restaurar os caminhos desses vínculos na pasta onde o IGP é aberto.

O IGP não substitui o IGZ como ficheiro de trabalho nem altera a lógica das referências externas: é um retrato transportável do conjunto no momento da exportação. Após a restauração, o projeto volta a trabalhar com o projeto principal e os respetivos IGZ separados, mantendo os estados, carregamento, sincronização e regras de edição dos vínculos.

## Painel

O painel chama-se **QTC Referências externas**. No topo tem sete ações, apresentadas como ícones com tooltip:

- **Adicionar referência IGZ** — regista um ou mais ficheiros e tenta carregá-los imediatamente.
- **Exportar projeto IGP** — cria um ficheiro comprimido `.igp` com o projeto principal e os vínculos disponíveis.
- **Exportar IGZ compacto** — cria, ao lado do projeto principal, uma cópia com o nome `nome_do_projeto_Compacto_AAMMDD-HHMMSS.igz`. Mantém no modelo os objetos de todas as referências carregadas, mas elimina os respetivos vínculos externos e os seus metadados. Para garantir que todos os objetos ficam incluídos, a exportação pede que todas as referências estejam carregadas; o projeto que permanece aberto não é modificado.
- **Abrir projeto IGP** — restaura o projeto e os vínculos com os nomes originais na pasta onde se encontra o `.igp`, atualiza os caminhos e abre o projeto restaurado numa nova janela.
- **Sincronizar todas** — descarrega e volta a carregar todas as referências que se encontram carregadas.
- **Atualizar estados** — reavalia os estados e remove bloqueios de edição que já expiraram.
- **Verificar caminhos** — confirma a disponibilidade dos ficheiros de origem.

Por baixo da barra existe o campo **Pesquisar referências**. A lista mostra por defeito as colunas **Referência externa** e **Estado**. Com o botão direito no cabeçalho podem ser mostradas as colunas auxiliares:

- **Extensão** — extensão do ficheiro.
- **Descrição** — campo de escrita manual, editável por duplo clique.
- **Atualização** — data e hora da última operação do vínculo.

As colunas Referência externa e Estado são sempre visíveis. A coluna Estado mostra apenas o ícone; o respetivo significado aparece no tooltip.

Qualquer operação que modifique vínculos ou objetos carregados apresenta uma janela modal de progresso: adicionar, carregar, descarregar, sincronizar, ligar, desligar, localizar, remover e sincronização automática na abertura do projeto. A janela identifica a referência que está a ser processada e mostra o progresso global.

## Estados

| Ícone | Estado | Significado |
| --- | --- | --- |
| Verde | Atualizado | O componente carregado corresponde à versão atual do IGZ de origem. |
| Amarelo | Desatualizado | O ficheiro de origem é mais recente do que a versão carregada. |
| Azul | Em atualização | A referência está a ser descarregada e carregada de novo. |
| Laranja | Em edição | A referência está aberta para edição através do gestor. O tooltip identifica o PC e o utilizador. |
| Cinzento | Não carregado | O vínculo existe, mas os seus objetos não estão no modelo central. |
| Vermelho | Indisponível | O ficheiro não existe ou não está acessível no caminho registado. |

Uma referência descarregada fica apresentada com texto esbatido.

Se uma referência estiver em edição mas o ficheiro já tiver sido gravado com alterações mais recentes, o estado apresentado é **Desatualizado** (amarelo), para indicar que o projeto central pode sincronizar a nova versão. Depois da sincronização, se a referência continuar aberta, volta a apresentar **Em edição** (laranja).

## Procedimento de utilização

### Adicionar uma referência

1. Abra o painel e selecione o ícone **Adicionar referência IGZ**.
2. Selecione um ou mais ficheiros `.igz`.
3. Cada ficheiro é registado uma única vez por caminho e é carregado de imediato no modelo central.
4. Se a importação falhar, o vínculo permanece registado como descarregado e é apresentada uma mensagem com o erro.

### Trabalhar com uma referência

Clique com o botão direito sobre a respetiva linha. As opções disponíveis dependem do estado do vínculo:

- **Carregar / Descarregar** — alterna a presença dos objetos no modelo central. Ao carregar, são importados os objetos da versão atual do ficheiro de origem.
- **Sincronizar** — substitui a versão carregada pela versão atual do ficheiro de origem. Esta ação só está disponível quando a referência está carregada.
- **Editar referência** — abre o ficheiro de origem numa nova janela do IngeTrazo, para que seja alterado no seu projeto próprio. A janela do projeto central permanece aberta e fica protegida contra fecho enquanto a edição estiver em curso.
- **Localizar…** — substitui o caminho do ficheiro; se a referência estiver carregada, volta a importá-la a partir da nova localização.
- **Remover** — descarrega os objetos daquele vínculo e apaga apenas o seu registo no projeto central. O ficheiro de origem não é apagado.

Ao clicar com o botão direito numa zona vazia da lista, ficam disponíveis **Adicionar…**, **Sincronizar tudo**, **Atualizar estados** e **Verificar**.

### Terminar a edição de uma referência

Ao usar **Editar referência**, a nova janela recebe, no topo da tab **QTC Referências externas**, o botão **Terminar edição, guardar e atualizar o projeto principal**. Se existirem alterações não guardadas, é apresentada a confirmação nativa: **Guardar** grava o IGZ antes de fechar; **Não guardar** fecha e perde as últimas alterações; **Cancelar** mantém a janela aberta. Ao terminar, o projeto central é notificado e, se continuar aberto, a referência é sincronizada automaticamente com uma janela de progresso.

A vista 3D desta janela mostra uma moldura azul-claro de 4 px e a etiqueta **Modo de edição** no canto superior esquerdo, para distinguir visualmente o ficheiro de referência do projeto central.

Enquanto uma referência estiver em edição, o projeto central não pode ser fechado nem os seus objetos podem ser alterados. A vista 3D fica em modo de consulta, permitindo apenas órbita e medições. Termine primeiro a edição pela ação indicada na janela da referência.

Enquanto um documento IGZ estiver aberto numa instalação com este plugin, o gestor cria junto do IGZ um ficheiro de presença JSON no formato `nome_sem_extensão_lock.json`, com o PC, utilizador e uma renovação periódica. Por isso, outros projetos que usem o mesmo caminho apresentam o estado laranja **Em edição** e identificam o editor no tooltip, mesmo que o ficheiro tenha sido aberto diretamente e não pelo menu **Editar referência**. O ficheiro é eliminado ao fechar normalmente o desenho ou ao terminar a edição. A verificação desta presença é feita a cada 5 segundos. Um bloqueio sem renovação durante 90 segundos deixa de ser considerado ativo; a ação **Atualizar estados** elimina esses bloqueios abandonados, permitindo recuperar de um encerramento inesperado sem afetar uma edição que ainda esteja ativa.

### Atualização automática

O estado é reavaliado ao abrir ou dar foco ao painel, ao mover o cursor para a sua área e a cada 10 minutos. O intervalo é configurável no início de `QTC_Referencias_externas.py`, através de `XREF_CHECK_INTERVAL_MS`.

Quando é aberto um projeto, o plugin verifica todos os vínculos carregados. As referências disponíveis que estejam desatualizadas são automaticamente sincronizadas. As referências descarregadas permanecem descarregadas até usar **Carregar**; nesse momento é sempre importada a versão mais recente. Ficheiros em falta não são sincronizados e mantêm o estado **Indisponível**. Depois de o projeto estar aberto, uma alteração ao ficheiro de origem apenas muda o estado para **Desatualizado**; a atualização passa a ser sempre manual, através de **Sincronizar** ou **Sincronizar todas**.

## Objetos carregados

Os objetos de uma referência carregada surgem como um componente no modelo central. Podem ser selecionados e fornecer snaps, permitindo coordenar, medir e desenhar com base neles.

Para permitir seleção e snaps, o componente não é bloqueado através da propriedade `locked`: na API atual essa propriedade também o retira do picking e dos snaps.

## Limitações do núcleo e da API

**O carregamento atual não é um XREF nativo.** A API disponível importa os objetos do IGZ para o modelo central; por isso, embora o plugin possa controlar o respetivo ciclo de carregamento e remoção, não consegue assegurar ao nível do núcleo que esses objetos nunca são serializados, alterados ou tratados como objetos locais.

Para um XREF real, o IngeTrazo precisa de uma API de **instância externa** no viewport: ler o `.IGZ`, renderizar os seus objetos e libertá-los, sem os inserir em `scene.groups` nem serializá-los no projeto principal.

### Evolução proposta: IGP integrado no IGZ

Como melhoria do núcleo, propõe-se que a capacidade atualmente oferecida pelo IGP seja integrada no próprio formato IGZ: um projeto poderia transportar internamente o projeto principal e os conteúdos das referências, mas preservar a lógica de vínculos externos como entidades independentes. Isto permitiria distribuir e abrir um projeto completo sem perder os estados de cada referência, os seus caminhos/origens, as operações de carregar, descarregar e sincronizar, nem as restrições de edição.

Essa evolução deve também permitir que vários utilizadores abram o mesmo projeto. Cada referência deve continuar a publicar o seu estado — atualizado, desatualizado, em atualização, não carregado, indisponível ou em edição — e aplicar as restrições correspondentes, em especial impedir edições concorrentes e informar quem está a editar. A integração no IGZ não deve transformar as referências em objetos locais sem controlo; deve manter a proveniência, o ciclo de vida e a colaboração por estados.

Enquanto essa API não existir, o bloqueio de edição pelo plugin é necessariamente parcial: pode orientar e controlar as operações do gestor, mas não substitui uma proteção aplicada pelo próprio núcleo do IngeTrazo.

### Bloqueio antes de abrir um ficheiro

O plugin consegue impedir uma segunda edição quando a abertura é iniciada pela ação **Editar referência**, pois pode verificar o ficheiro `nome_sem_extensão_lock.json` antes de criar a nova janela. Contudo, não existe atualmente uma API que permita intervir antes das aberturas nativas do IngeTrazo, como **Arquivo → Abrir**, **Arquivo → Nova janela**, arrastar um ficheiro ou abrir por duplo clique no Windows. Por isso, uma referência já em edição pode ainda ser aberta por esses caminhos; o plugin só consegue criar ou detetar a presença depois da abertura e apresentá-la no painel.

Para um bloqueio efetivo, o núcleo precisa de um *hook* de pré-abertura do documento, por exemplo `before_open_document(path)`, executado antes de carregar o IGZ e capaz de cancelar a abertura. Esse *hook* deve fornecer o caminho solicitado e aceitar uma resposta de bloqueio/mensagem do plugin, permitindo consultar o ficheiro `nome_sem_extensão_lock.json` e indicar o PC e utilizador que estão a editar a referência.

### Integridade em trabalho de rede

O mecanismo atual de presença usa um `heartbeat` renovado periodicamente e considera um bloqueio abandonado após 90 segundos sem renovação. Este valor é apenas uma recuperação prática para sessões interrompidas; **não garante exclusividade nem integridade numa falha de rede**. Numa partição de rede, dois computadores podem deixar de se ver, considerar o lock remoto expirado e gravar versões incompatíveis do mesmo IGZ.

Para garantir a integridade, o núcleo precisa de uma API de bloqueio partilhado com aquisição exclusiva, renovação e deteção de perda de ligação. Quando a ligação ficar incerta, o programa deve bloquear a gravação no ficheiro partilhado ou guardar apenas uma cópia local de recuperação. Ao recuperar a rede, deve comparar versão, data e hash do ficheiro antes de permitir a gravação; havendo alterações remotas, deve abrir um fluxo de resolução de conflito, nunca substituir automaticamente. A escrita deve ser atómica, através de um ficheiro temporário e substituição segura, com cópia de segurança. Em rede, a remoção de locks expirados deve exigir validação/decisão administrativa ou uma autoridade central, não apenas o decurso do timeout.

Como evolução recomendada, o IngeTrazo deve disponibilizar um modelo de colaboração centralizado, semelhante aos *worksets* do Revit. O utilizador reserva no servidor a edição de um vínculo e, enquanto essa reserva estiver ativa, nenhum outro utilizador pode editar o mesmo IGZ. Cada utilizador trabalha numa réplica local do projeto, mas só pode alterar o vínculo para o qual recebeu permissão de edição; os restantes vínculos permanecem em consulta.

Na sincronização com o servidor, o cliente devolve apenas o ficheiro IGZ que tem reservado para edição e recebe os restantes ficheiros do projeto que tenham sido atualizados por outros utilizadores. Após concluir as alterações, o autor deve poder **libertar a edição** no servidor, tornando o vínculo novamente editável pelos restantes membros da equipa. Esta API deve manter no servidor a autoria da reserva, a versão/base de sincronização e o estado de devolução, para impedir gravações concorrentes e permitir auditoria.

O estado **Em edição** obtém atualmente o nome de utilizador do sistema operativo. Para uma identificação consistente em redes, domínios e postos partilhados, o IngeTrazo deve permitir definir e disponibilizar um nome de utilizador nas preferências do próprio programa.

As referências IGZ aninhadas não são propagadas: ao adicionar uma referência ao projeto principal, são carregados apenas os seus objetos próprios; os vínculos que esse IGZ contenha são ignorados e nunca passam a ser carregados no projeto principal. Também não estão abrangidos por esta implementação caminhos relativos, prevenção de ciclos, renderização específica em layouts e a preservação de uma instância externa independente no ficheiro principal.

## Verificação recomendada

Antes de utilizar em produção, teste num projeto descartável:

1. Adicione um IGZ simples e confirme que os objetos e snaps estão disponíveis.
2. Guarde, feche e volte a abrir o projeto central; confirme a sincronização automática.
3. Altere e guarde o IGZ de origem numa segunda janela; confirme o estado amarelo e execute **Sincronizar**.
4. Descarregue e remova a referência; confirme que apenas o componente importado pelo vínculo é removido.
5. Mova ou elimine temporariamente o ficheiro de origem; confirme o estado vermelho e utilize **Localizar…**.

## Desenvolvimento

A implementação destina-se à API de plugins do IngeTrazo 0.5.8 e deve ser validada numa instalação real após alterações ao núcleo ou à API de extensões.

Documentação de plugins do IngeTrazo: <https://github.com/ingelibre/ingetrazo/blob/main/docs/plugins.md>
